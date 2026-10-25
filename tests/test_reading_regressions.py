import ast
import html
import logging
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import public_sources
from research_search import investigate, SearchBudget


class ReadingRegressions(unittest.TestCase):
    def test_installed_trafilatura_document_is_used_instead_of_navigation(self):
        import trafilatura
        tree = ast.parse(Path('network_agent/core.py').read_text(encoding='utf-8'))
        function = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_extract_content')
        namespace = {'trafilatura': trafilatura, 'logger': logging.getLogger('test'),
                     '_html_to_text': Mock(side_effect=AssertionError('Unexpected HTML fallback'))}
        exec(compile(ast.Module(body=[function], type_ignores=[]), 'extract-test', 'exec'), namespace)
        raw = ('<html><body><nav>MENU</nav><article>'+('<p>Useful technical documentation paragraph.</p>'*40)+'</article></body></html>').encode()
        text, *_ = namespace['_extract_content'](raw, 'https://example.com/')
        self.assertIn('Useful technical', text)
        self.assertNotIn('MENU', text)

    def test_requested_section_after_twenty_thousand_chars_reaches_evaluator(self):
        url = 'https://example.com/needle'
        text = 'needle navigation and introduction. '*1000+'\nCookie Authentication\nImplementation broken and currently unavailable.\n'
        reader = Mock(return_value={'text_content': text})
        calls = []
        def planner(query, evidence, *args):
            calls.append(evidence)
            if len(calls) == 1:
                return {'decision': 'read', 'questions': ['cookies?'],
                        'read_requests': [{'url': url, 'focus': 'Cookie Authentication broken unavailable'}]}
            return {'decision': 'search', 'questions': ['more details']}
        result = investigate('needle', search=lambda *a: {'status':'ok', 'items':[{'url':url,'title':'needle'}]},
                             read=reader, planner=planner, providers=('test',),
                             budget=SearchBudget(max_pages=1,max_rounds=2))
        reader.assert_called_once()
        self.assertEqual(result.report['refocused_reads'], 1)
        self.assertIn('currently unavailable', calls[1][0]['body'])
        self.assertIn('currently unavailable', result[0]['body'])
        self.assertTrue(calls[1][0]['excerpted'])

    def test_objective_and_video_coverage_are_visible_to_evaluator(self):
        url='https://youtu.be/abcdefghijk'
        text='needle introduction. '*1000+'\n[00:29:50] RareAnswer is in the last minute.'
        planner=Mock(return_value={})
        result=investigate('needle', objective='RareAnswer last minute',
            search=lambda *a: {'status':'ok','items':[{'url':url,'title':'needle'}]},
            read=lambda u: {'text_content':text,'content_kind':'transcript','transcript_truncated':True,'automatic_captions':True},
            planner=planner,providers=('test',),budget=SearchBudget(max_rounds=1))
        evidence=planner.call_args.args[1][0]
        self.assertIn('RareAnswer',evidence['body'])
        self.assertTrue(evidence['transcript_truncated'])
        self.assertEqual(evidence['content_kind'],'transcript')
        self.assertTrue(result[0]['excerpted'])

    def test_rereading_cannot_exceed_read_budget(self):
        planner=Mock(return_value={'read_requests':[{'url':'https://example.com/','focus':'different section'}]})
        reader=Mock(return_value={'text_content':'needle '*6000})
        result=investigate('needle',search=lambda *a:{'status':'ok','items':[{'url':'https://example.com/','title':'needle'}]},
                           read=reader,planner=planner,providers=('test',),budget=SearchBudget(max_reads=1))
        self.assertEqual(result.report['reads'],1)
        self.assertEqual(reader.call_count,1)

    def test_search_budget_does_not_block_local_passage_reading(self):
        url='https://example.com/'
        reader=Mock(return_value={'text_content':'needle '*7000+'Cookie Authentication broken'})
        planner=Mock(return_value={'read_requests':[{'url':url,'focus':'Cookie Authentication broken'}]})
        result=investigate('needle',search=lambda *a:{'status':'ok','items':[{'url':url,'title':'needle'}]},
                           read=reader,planner=planner,providers=('test','second'),
                           budget=SearchBudget(max_requests=1,max_reads=2,max_rounds=2))
        self.assertEqual(result.report['requests'],1)
        self.assertEqual(result.report['refocused_reads'],1)
        self.assertIn('Authentication broken',result[0]['body'])
        reader.assert_called_once()

    def test_video_profiles_do_not_consume_video_reading_budget(self):
        self.assertFalse(public_sources.useful_candidate('https://www.youtube.com/channel/UC123'))
        self.assertFalse(public_sources.useful_candidate('https://www.tiktok.com/@author'))
        self.assertTrue(public_sources.useful_candidate('https://www.youtube.com/shorts/abcdefghijk'))
        self.assertTrue(public_sources.useful_candidate('https://www.tiktok.com/@author/video/123'))

    def test_video_format_without_size_does_not_default_to_highest_resolution(self):
        from media_worker import select_audio
        high={'url':'https://cdn.test/high','vcodec':'h264','acodec':'aac','width':1920,'height':1080}
        low={**high,'url':'https://cdn.test/low','width':640,'height':360}
        selected,_=select_audio({'duration':60,'formats':[high,low]})
        self.assertEqual(selected['url'],low['url'])
        selected,_=select_audio({'duration':60,'formats':[{**high,'tbr':5000},{**low,'tbr':300}]})
        self.assertEqual(selected['url'],low['url'])

    def test_media_relevance_handles_plurals_and_compound_hashtags(self):
        from media_worker import media_matches_query
        self.assertTrue(media_matches_query('рецепт готовим еда', '#готовимдома #рецепты'))
        self.assertTrue(media_matches_query('Какаду AI', 'Kakadu — помощник'))
        self.assertFalse(media_matches_query('Какаду AI', 'Photoshop AI editor'))
        self.assertFalse(media_matches_query('рецепт', 'Python while loops'))

    def test_tiktok_subtitle_extraction_is_enabled_without_downloading_video(self):
        from unittest.mock import patch, MagicMock
        from media_worker import run
        import time
        seen=[]
        def sdk(options):
            seen.append(options)
            context=MagicMock()
            client=context.__enter__.return_value
            client.cookiejar=[]
            client.extract_info.return_value={'subtitles':{'ru':[{'ext':'vtt','data':'WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nUseful speech'}]}} if options.get('writesubtitles') else {}
            return context
        with patch('yt_dlp.YoutubeDL',side_effect=sdk):
            result=run({'url':'https://www.tiktok.com/@test/video/123','timeout':5,'deadline':time.monotonic()+5,'allow_audio':False,'proxy':''})
        self.assertEqual(result['content_kind'],'transcript')
        self.assertIn('Useful speech',result['text_content'])
        self.assertTrue(seen[0]['skip_download'])


if __name__ == '__main__':
    unittest.main()
