"""Go → Python HTTP end-to-end acceptance using disposable synthetic accounts.

Run against an isolated local offline instance; no production or remote URLs.
This is an HTTP integration test, not a replacement for real-device browser QA.
"""
import argparse
import http.cookiejar
import json
import re
import time
import uuid
from urllib.error import HTTPError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:8080')
args = parser.parse_args()
assert urlparse(args.url).hostname in {'localhost', '127.0.0.1'}, 'local test only'
period = '2026-09'

class Browser:
    def __init__(self, name):
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))
        self.csrf = ''
        self.request('/register', {'username': name, 'password': 'SyntheticTestOnly2026!'})
        html = self.request('/profile')[1]
        match = re.search(r'name="csrf" value="([^"]+)"', html)
        assert match, 'registration/login failed'
        self.csrf = match[1]

    def request(self, path, values=None):
        headers = {'HX-Request': 'true'} if self.csrf else {}
        data = None
        if values is not None:
            values = {**values, 'csrf': self.csrf, 'period': period}
            data = urlencode(values).encode()
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            with self.opener.open(Request(args.url+path, data=data, headers=headers), timeout=15) as r:
                return r.headers, r.read().decode()
        except HTTPError as e:
            raise AssertionError(f'{path}: HTTP {e.code}: {e.read().decode()}') from e

    def agent(self, kind, **values):
        values.setdefault('request_id', uuid.uuid4().hex)
        _, html = self.request('/agent/'+kind, values)
        job = re.search(r'hx-get="(/jobs/[^"]+)"', html)
        assert job, html
        assert 'hx-push-url="false"' in html
        for _ in range(150):
            headers, html = self.request(job[1])
            location = headers.get('HX-Location')
            if location:
                path = json.loads(location)['path']
                return self.request(path)[1]
            time.sleep(.1)
        raise AssertionError('job timed out')

suffix = uuid.uuid4().hex[:8]
first = Browser('http_qa_'+suffix)
second = Browser('http_other_'+suffix)
first.agent('manual', action='confirm', occupation='合成测试', income_cents='8000.29',
            income_stable='true', debt_cents='0', reserve_cents='1000', horizon_months='36', max_loss_pct='0')
for day, direction, amount, category in [('01','income','8000.29','工资'),('02','expense','2000.29','居住'),('03','transfer','100.01','转账')]:
    first.request('/ledger', {'date':period+'-'+day, 'direction':direction, 'amount':amount, 'category':category})
ledger = first.request('/ledger?period='+period)[1]
assert '¥6000.00' in ledger and ledger.count('class="transaction-row"') == 3
first.agent('month')
html = first.agent('month', action='finish', non_invested_cents='6000', invested_cents='0', has_investments='false')
assert '确认这份实况' in html
first.agent('month', action='confirm')
html = first.agent('plan')
assert '确认这份方案' in html
html = first.agent('plan', action='edit', message='优先补足预备金')
assert '确认这份方案' in html
first.agent('plan', action='confirm')
html = first.agent('chat', message='解释预备金安排')
assert '解释预备金安排' in html
html = first.agent('summary')
assert '确认这份复盘' in html
first.agent('summary', action='confirm')
assert '输入资料或复盘经验已更新' in first.request('/plan?period='+period)[1]
other = second.request('/ledger?period='+period)
assert other[0].get('HX-Location') and '8000.29' not in other[1]
print('HTTP_E2E_PASSED: 2 accounts; profile/null/zero; exact cents; transfer; month; plan edit/confirm; chat; review; feedback; isolation')
