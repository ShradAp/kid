"""Focused live checks; no model call or account login."""
import ast
import html as html_mod
import json
import logging
from pathlib import Path
import re
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from guest_transport import GuestSession
import public_sources
from research_search import relevant_fragment
import trafilatura

logging.disable(logging.CRITICAL)
namespace=globals().copy()
tree=ast.parse((ROOT/'network_agent/core.py').read_text(encoding='utf-8'))
functions=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in ('_html_to_text','_meta_content','_extract_content')]
namespace['logger']=logging.getLogger('check')
exec(compile(ast.Module(body=functions,type_ignores=[]),'extract-check','exec'),namespace)
report={'pages':[],'videos':[]}
for url,focus in [('https://github.com/jdepoix/youtube-transcript-api','Cookie Authentication broken currently not available RequestBlocked IpBlocked'),
                  ('https://www.perplexity.ai/help-center/en/articles/10354919-what-advanced-ai-models-are-included-in-my-subscription','Pro and Max October 17 2026 GPT-6.1 Sonnet')]:
    start=time.monotonic()
    try:
        with GuestSession(18,allowed_domains=((__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(url).hostname),),max_requests=3,max_bytes=3000000) as session:
            response=session.get(url)
            response.raise_for_status()
            text,title,*_=namespace['_extract_content'](response.content,url)
        excerpt=relevant_fragment(text,focus,4000)
        report['pages'].append({'url':url,'chars':len(text),'title':title,'seconds':round(time.monotonic()-start,2),
                                'focus_excerpt':excerpt,'contains_cookie_warning':'currently not available' in excerpt,
                                'contains_fresh_models':'GPT-6.1' in excerpt})
    except Exception as exc:
        report['pages'].append({'url':url,'error':type(exc).__name__})
    print(json.dumps({'event':'page_done','url':url}),flush=True)

try:
    from playwright.sync_api import sync_playwright
    tree=ast.parse((ROOT/'network_agent/browser.py').read_text(encoding='utf-8'))
    assignment=next(n for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='UNIVERSAL_EXTRACTOR_JS' for t in n.targets))
    javascript=ast.literal_eval(assignment.value)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        page=browser.new_page()
        page.set_content('<html><body><article>'+''.join(f'<p>Document paragraph {i}: important technical content.</p>' for i in range(1500))+'<p>LATE_SECTION_SENTINEL</p></article></body></html>')
        data=page.evaluate(javascript)
        browser.close()
    report['browser']={'chars':len(data.get('text_content','')),'late_section_preserved':'LATE_SECTION_SENTINEL' in data.get('text_content',''),'source_truncated':data.get('source_truncated')}
except Exception as exc:
    report['browser']={'error':type(exc).__name__}

for url in ['https://www.youtube.com/watch?v=5HqcnTP_hdQ','https://www.youtube.com/watch?v=Ok7Q2LGvQPI',
            'https://www.tiktok.com/@chopocompik_off/video/7695059987336416544']:
    start=time.monotonic()
    data=public_sources.read_public(url,timeout=25,query='Какаду Kakadu AI',allow_audio=True)
    report['videos'].append({'url':url,'seconds':round(time.monotonic()-start,2),'chars':len(data.get('text_content','')),
        **{k:data[k] for k in ('content_kind','caption_status','caption_language','automatic_captions','transcript_truncated','processed_audio_seconds','media_status','error') if k in data}})
    print(json.dumps({'event':'video_done',**report['videos'][-1]}),flush=True)

baseline=json.loads((ROOT/'reports/search-evaluation-2026-10-25.json').read_text(encoding='utf-8'))
media=[s for r in baseline['runs'] for s in r['report']['sources'] if s.get('platform') in ('youtube','tiktok') and s['read_status']!='snippet_only']
from collections import Counter
report['previous_runs']={'attempts':len(media),'kinds':dict(Counter(s.get('content_kind','unavailable') for s in media)),
    'unique_transcript_urls':len({s['url'] for s in media if s.get('content_kind')=='transcript'})}
out=ROOT/'reports/reading-check-2026-10-25.json'
out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'event':'complete','previous_runs':report['previous_runs'],'browser':report['browser']}),flush=True)
