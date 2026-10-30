from datetime import datetime, timezone
import re

FEED = 'agent.github-pull-requests'
SCHEMA = {'type':'object','additionalProperties':False,'required':['observedAt','expectedIntervalSec','repositories'],'properties':{'observedAt':{'type':'string','format':'date-time'},'expectedIntervalSec':{'type':'integer','minimum':1},'repositories':{'type':'array','maxItems':10,'items':{'type':'object','additionalProperties':False,'required':['name','status','message','totalOpen','pullRequests'],'properties':{'name':{'type':'string','maxLength':200},'status':{'enum':['ok','unavailable']},'message':{'type':'string','maxLength':300},'totalOpen':{'type':['integer','null'],'minimum':0},'pullRequests':{'type':'array','maxItems':300,'items':{'type':'object','additionalProperties':False,'required':['number','title','author','draft','review','checks','merge'],'properties':{'number':{'type':'integer','minimum':1},'title':{'type':'string','maxLength':500},'author':{'type':'string','maxLength':100},'draft':{'type':['boolean','null']},'review':{'enum':['approved','changes_requested','review_required','pending','none','unknown']},'checks':{'enum':['passing','failing','pending','none','unknown']},'merge':{'enum':['mergeable','conflicting','unknown']}}}}}}}}

class ReadFailure(Exception):
    pass


def main(inputs, job):
    repos = inputs['repositories']
    interval = inputs['refresh_seconds']
    if not isinstance(repos, list) or not 1 <= len(repos) <= 10 or len(set(repos)) != len(repos):
        raise ValueError('Invalid repositories')
    if not isinstance(interval, int) or not 300 <= interval <= 86400:
        raise ValueError('Invalid refresh interval')
    for repo in repos:
        if not isinstance(repo, str) or not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo) or repo.split('/')[1] in ('.', '..'):
            raise ValueError('Invalid repository name')
    requests = 0

    def get(url):
        nonlocal requests
        if requests >= 95:
            raise ReadFailure('Request budget reached')
        requests += 1
        try:
            response = job.http.request('GET', url)
        except Exception:
            raise ReadFailure('GitHub request unavailable')
        if response.get('status') != 200:
            raise ReadFailure('GitHub HTTP ' + str(response.get('status')))
        return response.get('json')

    def pages(url, key=None, max_pages=5, per_page=100):
        result = []
        for page in range(1, max_pages + 1):
            body = get(url + ('&' if '?' in url else '?') + 'per_page=' + str(per_page) + '&page=' + str(page))
            batch = body.get(key) if key and isinstance(body, dict) else body
            if not isinstance(batch, list):
                raise ReadFailure('Unexpected GitHub response')
            result.extend(batch)
            if key and isinstance(body, dict) and isinstance(body.get('total_count'), int):
                if len(result) >= body['total_count']:
                    return result
            if len(batch) < per_page:
                if key and isinstance(body, dict) and body.get('total_count', len(result)) > len(result):
                    raise ReadFailure('Incomplete GitHub response')
                return result
        raise ReadFailure('Pagination limit reached')

    def reviews(base, number, detail):
        entries = pages(base + '/pulls/' + str(number) + '/reviews')
        latest = {}
        for entry in sorted(entries, key=lambda x:x.get('id', 0)):
            state = entry.get('state')
            user = entry.get('user') or {}
            login = user.get('login')
            if login and state in ('APPROVED', 'CHANGES_REQUESTED', 'DISMISSED'):
                latest[login] = state
        states = list(latest.values())
        if 'CHANGES_REQUESTED' in states:
            return 'changes_requested'
        if 'APPROVED' in states:
            return 'approved'
        if detail.get('requested_reviewers') or detail.get('requested_teams'):
            return 'pending'
        return 'none'

    def checks(base, sha):
        if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-fA-F]{40,64}', sha):
            raise ReadFailure('Head commit unavailable')
        runs = pages(base + '/commits/' + sha + '/check-runs?filter=latest', key='check_runs')
        status = get(base + '/commits/' + sha + '/status?per_page=100')
        if not isinstance(status, dict) or not isinstance(status.get('statuses'), list) or not isinstance(status.get('total_count'), int):
            raise ReadFailure('Commit statuses unavailable')
        states = []
        if status['total_count'] > 0:
            state = status.get('state')
            states.append({'success':'passing','failure':'failing','error':'failing','pending':'pending'}.get(state, 'unknown'))
        for run in runs:
            if run.get('status') != 'completed':
                states.append('pending' if run.get('status') in ('queued','in_progress','waiting','requested','pending') else 'unknown')
            elif run.get('conclusion') in ('success','neutral','skipped'):
                states.append('passing')
            elif run.get('conclusion') in ('failure','cancelled','timed_out','action_required','startup_failure','stale'):
                states.append('failing')
            else:
                states.append('unknown')
        for candidate in ('failing','unknown','pending'):
            if candidate in states:
                return candidate
        return 'passing' if states else 'none'

    output = []
    for repo in repos:
        base = 'https://api.github.com/repos/' + repo
        row = {'name':repo,'status':'unavailable','message':'','totalOpen':None,'pullRequests':[]}
        try:
            listed = pages(base + '/pulls?state=open&sort=created&direction=desc', max_pages=10, per_page=30)
            if len(listed) > 300:
                raise ReadFailure('PR listing exceeds display limit')
            seen = set()
            for pull in listed:
                if not isinstance(pull, dict) or not isinstance(pull.get('number'), int) or pull.get('state') != 'open':
                    raise ReadFailure('Unexpected PR listing')
                number = pull['number']
                if number in seen:
                    continue
                seen.add(number)
                detail_ok = True
                try:
                    detail = get(base + '/pulls/' + str(number))
                    if not isinstance(detail, dict) or detail.get('number') != number or detail.get('state') not in ('open','closed'):
                        raise ReadFailure('PR detail unavailable')
                    if detail.get('state') == 'closed':
                        continue
                except ReadFailure:
                    detail = pull
                    detail_ok = False
                item = {'number':number,'title':str(detail.get('title') or '')[:500],'author':str((detail.get('user') or {}).get('login') or '')[:100],'draft':detail.get('draft') if isinstance(detail.get('draft'), bool) else None,'review':'unknown','checks':'unknown','merge':'unknown'}
                if detail_ok:
                    mergeable = detail.get('mergeable')
                    item['merge'] = 'mergeable' if mergeable is True else 'conflicting' if mergeable is False else 'unknown'
                    try:
                        item['review'] = reviews(base, number, detail)
                    except ReadFailure:
                        pass
                    try:
                        item['checks'] = checks(base, (detail.get('head') or {}).get('sha'))
                    except ReadFailure:
                        pass
                row['pullRequests'].append(item)
            row['totalOpen'] = len(row['pullRequests'])
            row['status'] = 'ok'
            if any('unknown' in (p['review'],p['checks'],p['merge']) for p in row['pullRequests']):
                row['message'] = 'Some status fields are unknown; GitHub may be computing mergeability or a detail endpoint was unavailable.'
        except ReadFailure as exc:
            row['message'] = str(exc)[:300] + '; not a zero-PR result.'
            row['pullRequests'] = []
        output.append(row)
    data = {'observedAt':datetime.now(timezone.utc).isoformat(),'expectedIntervalSec':interval,'repositories':output}
    job.feed.publish(FEED, data, expected_interval_seconds=interval, schema=SCHEMA)
    return {'repositories':[{'name':r['name'],'status':r['status'],'open':r['totalOpen']} for r in output],'requests':requests}
