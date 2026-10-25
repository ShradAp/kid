"""Small guest-only TikTok batch; no author crawling or saved login cookies."""
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit, urlunsplit

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from guest_transport import private_settings
import search_tools
from public_sources import read_public

def main():
    cfg=private_settings()
    search_tools.load_config=lambda:cfg
    cases=[]
    seen=set()
    for query,focus in [('site:tiktok.com нейросеть какаду AI','Какаду Kakadu нейросеть AI'),
                        ('site:tiktok.com python coding tutorial','python coding code'),
                        ('site:tiktok.com рецепт готовим','рецепт готовим еда'),
                        ('site:tiktok.com science explained','science explained')]:
        result=search_tools._serper_search(query,10,'ru',1,10,detailed=True)
        print(json.dumps({'event':'discovery','status':result.get('status'),'items':len(result.get('items',[]))}),flush=True)
        added=0
        for item in result.get('items',[]):
            url=item.get('url','')
            if not re.fullmatch(r'https://(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+(?:\?[^\s]*)?',url):
                continue
            parts=urlsplit(url);url=urlunsplit((parts.scheme,parts.netloc,parts.path,'',''))
            if url not in seen:
                seen.add(url)
                cases.append({'url':url,'focus':focus,'title':item.get('title',''),'snippet':item.get('body','')})
                added+=1
            if added>=2:
                break
    old='https://www.tiktok.com/@chopocompik_off/video/7695059987336416544'
    if old not in seen:
        cases.append({'url':old,'focus':'фотошоп Photoshop','title':'Контроль предыдущего превышения 8 МБ'})
    out=ROOT/'reports/tiktok-batch-2026-10-25.json'
    result={'limits':{'seconds_per_video':25,'media_bytes':8000000,'audio_seconds':90,'video_duration_seconds':180},
            'note':'One attempt per discovered video. No login, OCR, author traversal or model API calls.', 'runs':[]}
    for case in cases[:8]:
        print(json.dumps({'event':'start','url':case['url']}),flush=True)
        start=time.monotonic()
        data=read_public(case['url'],timeout=25,query=case['focus'],allow_audio=True)
        row={**case,'seconds':round(time.monotonic()-start,2),'chars':len(data.get('text_content','')),
             **{k:data[k] for k in ('content_kind','caption_status','caption_language','caption_provider','automatic_captions',
                                   'transcript_truncated','processed_audio_seconds','media_status','media_download_errors','error') if k in data},
             'text':data.get('text_content','')}
        result['runs'].append(row)
        out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps({'event':'done',**{k:row.get(k) for k in ('url','seconds','chars','content_kind','media_status','error')}}),flush=True)
    print(json.dumps({'event':'complete','videos':len(result['runs'])}),flush=True)

if __name__=='__main__':
    main()
