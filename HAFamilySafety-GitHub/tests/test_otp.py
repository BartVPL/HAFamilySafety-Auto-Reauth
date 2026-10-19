"""Offline test of the production OTP parser, without Gmail API dependencies."""
import ast
import re
from pathlib import Path
source = Path(__file__).resolve().parents[1] / 'src/gmail_setup/familysafety_gmail/gmail_reader.py'
tree = ast.parse(source.read_text())
fn = next(x for x in tree.body if isinstance(x, ast.FunctionDef) and x.name == 'extract_security_codes')
module = ast.Module(body=[fn], type_ignores=[])
namespace = {'re': re}
exec(compile(ast.fix_missing_locations(module), str(source), 'exec'), namespace)
extract_security_codes = namespace['extract_security_codes']
assert extract_security_codes('Twój jednorazowy kod: 123456\nhttps://go.microsoft.com/?LinkId=521839') == ['123456']
assert extract_security_codes('Your single-use code: 654321') == ['654321']
assert extract_security_codes('LinkId=521839') == []
assert len(extract_security_codes('Twój jednorazowy kod: 123456\nTwój jednorazowy kod: 654321')) == 2
print('OTP parser offline tests: PASS')
