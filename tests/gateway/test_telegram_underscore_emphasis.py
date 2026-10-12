"""Use the actual Telegram adapter without Telegram or model calls."""
import unittest
from plugins.platforms.telegram.adapter import TelegramAdapter,_strip_mdv2

class FormattingTests(unittest.TestCase):
 def setUp(self):self.adapter=object.__new__(TelegramAdapter)
 def test_grace_example(self):
  raw='_Good news and a wrinkle already — let me dig deeper before I say anything for sure._'
  self.assertEqual(self.adapter.format_message(raw),'_Good news and a wrinkle already — let me dig deeper before I say anything for sure\\._')
 def test_both_emphasis_spellings(self):
  self.assertEqual(self.adapter.format_message('_one_ and *two*, __three__ and **four**.'),'_one_ and _two_, *three* and *four*\\.')
 def test_identifiers_stay_literal(self):
  raw='my_file_name user_id file__version__name'
  self.assertEqual(self.adapter.format_message(raw),'my\\_file\\_name user\\_id file\\_\\_version\\_\\_name')
 def test_code_is_unchanged(self):
  self.assertEqual(self.adapter.format_message('`_literal_` and ```\n__literal__\n```'),'`_literal_` and ```\n__literal__\n```')
 def test_underscore_inside_emphasized_phrase(self):
  self.assertEqual(self.adapter.format_message('_use my_file_name_'),'_use my\\_file\\_name_')
 def test_link_url_underscores(self):
  self.assertEqual(self.adapter.format_message('[memo](https://example.com/my_file_name)'),'[memo](https://example.com/my_file_name)')
 def test_whitespace_and_escaped_delimiters_stay_literal(self):
  for raw in ['_ spaced _','__ spaced __',r'\_literal\_']:
   with self.subTest(raw=raw):self.assertEqual(self.adapter.format_message(raw),raw.replace('\\','\\\\').replace('_','\\_'))
 def test_plain_fallback_removes_emphasis(self):
  self.assertEqual(_strip_mdv2(self.adapter.format_message('_Hello._ and __news__')),'Hello. and news')

if __name__=='__main__':unittest.main()
