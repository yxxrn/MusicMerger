"""Bounded joint CTC repair of supported ASR boundary conflicts, not lyric inference."""
import copy
import math

from .fallback import supported

MAX_OVERLAP = 1.0
MAX_DISPLACEMENT = 2.0
MIN_SCORE = .5
MIN_SCORED_FRACTION = .8


def repair_lines(lines, duration, infer):
    """infer(start,end,words) returns absolute-second CTC spans and greedy text."""
    result = copy.deepcopy(lines)
    retained = [i for i, row in enumerate(lines) if not row.get('omitted')]
    groups = []
    for left, right in zip(retained, retained[1:]):
        a, b = lines[left], lines[right]
        if a['wend'] <= b['wstart'] + .000001:
            continue
        if (right != left+1 or not supported(a,duration) or not supported(b,duration)
                or not a['wstart'] < b['wstart'] < a['wend'] < b['wend']
                or a['wend']-b['wstart'] > MAX_OVERLAP+.000001):
            raise ValueError('Konflik ASR bukan batas lokal yang aman untuk koreksi CTC')
        if groups and groups[-1][-1] == left:
            groups[-1].append(right)
        else:
            groups.append([left,right])
    reports = []
    for group in groups:
        first,last = group[0],group[-1]
        start = max(0,lines[first]['wstart']-.6)
        end = min(duration,lines[last]['wend']+.6)
        previous = [i for i in retained if i < first]
        following = [i for i in retained if i > last]
        if previous:
            start = max(start,lines[previous[-1]]['wend'])
        if following:
            end = min(end,lines[following[0]]['wstart'])
        if not 0 < end-start <= 30:
            raise ValueError('Jendela koreksi CTC harus <=30 detik')
        tokens = [word for i in group for word in lines[i]['words_text']]
        spans, transcript = infer(start,end,tokens)
        if len(spans) != len(tokens):
            raise ValueError('Jumlah kata koreksi CTC tidak cocok')
        previous_end = start
        for span in spans:
            a,b,score = (span.get(key) for key in ('start','end','score'))
            if (not all(isinstance(v,(float,int)) and math.isfinite(v) for v in (a,b,score))
                    or not previous_end <= a < b <= end+.000001 or not 0 <= score <= 1):
                raise ValueError('Rentang/score koreksi CTC tidak valid')
            previous_end=b
        cursor=0
        updates=[]
        for i in group:
            row=lines[i]; count=row['nwords']; selected=spans[cursor:cursor+count]; cursor+=count
            scores=[span['score'] for span in selected]
            if (sum(s >= MIN_SCORE for s in scores) < MIN_SCORED_FRACTION*count
                    or sum(scores)/count < MIN_SCORE
                    or max(abs(selected[0]['start']-row['wstart']),
                           abs(selected[-1]['end']-row['wend'])) > MAX_DISPLACEMENT):
                raise ValueError(f'Bukti CTC koreksi baris {i+1} kurang kuat; perlu review audio')
            words=[[round(s['start']*1000,3),round(s['end']*1000,3)] for s in selected]
            result[i].update(words=words,wstart=words[0][0]/1000,wend=words[-1][1]/1000,
                             provenance=['ctc_boundary_repair']*count,estimated_words=0,
                             issues=['acoustic_boundary_repair_needs_listening_review'],needs_review=True)
            updates.append(dict(index=i,before=[row['wstart'],row['wend']],
                                after=[result[i]['wstart'],result[i]['wend']],words=words,scores=scores))
        reports.append(dict(window=[start,end],indices=group,greedy_transcript=transcript,lines=updates))
    if any(result[a]['wend'] > result[b]['wstart']+.000001 for a,b in zip(retained,retained[1:])):
        raise ValueError('Koreksi CTC masih bertabrakan dengan baris tetangga')
    return result,reports


def main():
    import argparse
    import json
    from pathlib import Path
    import subprocess
    import sys
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('reference',type=Path)
    parser.add_argument('--audio',required=True,type=Path)
    parser.add_argument('--model-dir',required=True,type=Path)
    parser.add_argument('--out',required=True,type=Path)
    args=parser.parse_args()
    from .renderer import audio_fingerprint,ffprobe_duration
    from .acoustic import ctc_word_spans
    reference=json.loads(args.reference.read_text(encoding='utf-8'))
    if args.out.exists():
        parser.error('Output koreksi sudah ada')
    if audio_fingerprint(args.audio)['sha256'] != reference['analysis_audio_sha256']:
        parser.error('Audio analisis tidak cocok dengan referensi')
    sys.modules['torchaudio']=None
    import numpy as np
    import torch
    from transformers import Wav2Vec2ForCTC,Wav2Vec2Processor
    torch.set_num_threads(6)
    processor=Wav2Vec2Processor.from_pretrained(args.model_dir,local_files_only=True)
    model=Wav2Vec2ForCTC.from_pretrained(args.model_dir,local_files_only=True).eval()
    frame_seconds=math.prod(model.config.conv_stride)/16000
    def infer(start,end,tokens):
        pcm=subprocess.check_output(['ffmpeg','-v','error','-nostdin','-ss',str(start),'-i',str(args.audio),
             '-t',str(end-start),'-ac','1','-ar','16000','-f','f32le','pipe:1'])
        audio=np.frombuffer(pcm,dtype=np.float32)
        with torch.inference_mode():
            logits=model(**processor(audio,sampling_rate=16000,return_tensors='pt')).logits[0]
        spans=ctc_word_spans(logits.log_softmax(-1).numpy(),tokens,processor.tokenizer.get_vocab(),model.config.pad_token_id)
        return [dict(start=start+s['start_frame']*frame_seconds,end=start+s['end_frame']*frame_seconds,
                     score=s['score']) for s in spans],processor.decode(logits.argmax(-1).tolist())
    duration=ffprobe_duration(args.audio)
    if abs(duration-reference['song_duration']) > .03:
        parser.error('Durasi audio koreksi tidak cocok')
    repaired,reports=repair_lines(reference['lines'],duration,infer)
    reference.update(lines=repaired,boundary_repair=dict(windows=reports,
        max_overlap=MAX_OVERLAP,max_displacement=MAX_DISPLACEMENT,min_score=MIN_SCORE,
        min_scored_fraction=MIN_SCORED_FRACTION,review_required=True,
        model_sha256=audio_fingerprint(args.model_dir/'model.safetensors')['sha256']))
    with args.out.open('x',encoding='utf-8') as stream:
        json.dump(reference,stream,ensure_ascii=False,indent=2)
    print(f'Koreksi CTC: {len(reports)} jendela; validasi timing penuh tetap wajib.',flush=True)


if __name__ == '__main__':
    main()
