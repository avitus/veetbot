from datetime import datetime, timezone
import re

def main(inputs, job):
    repos = inputs['repositories']
    interval = inputs['refresh_seconds']
    if not 1 <= len(repos) <= 10 or len(set(repos)) != len(repos):
        raise ValueError('Invalid repositories')
    for repo in repos:
        if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+', repo) or repo.split('/')[1] in ('.','..'):
            raise ValueError('Invalid repository')
    used = [0]
    def get(url):
        used[0] += 1
        if used[0] > 45:
            raise RuntimeError('Request budget exhausted')
        response = job.http.request('GET', url)
        if response.get('status') != 200:
            raise RuntimeError('GitHub HTTP ' + str(response.get('status')))
        return response.get('json')
    def listing(url, key=None):
        result = []
        for page in range(1, 31):
            body = get(url + ('&' if '?' in url else '?') + 'per_page=10&page=' + str(page))
            rows = body.get(key) if key and isinstance(body, dict) else body
            if not isinstance(rows, list):
                raise RuntimeError('Unexpected list response')
            result.extend(rows)
            if key and isinstance(body.get('total_count'), int) and len(result) >= body['total_count']:
                return result
            if len(rows) < 10:
                if key and body.get('total_count', len(result)) > len(result):
                    raise RuntimeError('Incomplete list')
                return result
        raise RuntimeError('List limit reached')
    output = []
    for repo in repos:
        base = 'https://api.github.com/repos/' + repo
        item = {'name':repo,'status':'unavailable','message':'','totalOpen':None,'pullRequests':[]}
        try:
            pulls = listing(base + '/pulls?state=open&sort=created&direction=desc')
            seen = set()
            for pull in pulls:
                number = pull.get('number')
                if not isinstance(number, int) or pull.get('state') != 'open':
                    raise RuntimeError('Invalid open PR listing')
                if number in seen:
                    continue
                seen.add(number)
                pr = {'number':number,'title':str(pull.get('title') or '')[:500],'author':str((pull.get('user') or {}).get('login') or '')[:100],'draft':pull.get('draft') if isinstance(pull.get('draft'),bool) else None,'review':'unknown','checks':'unknown','merge':'unknown'}
                detail = None
                try:
                    detail = get(base + '/pulls/' + str(number))
                    if not isinstance(detail,dict) or detail.get('number') != number or detail.get('state') not in ('open','closed'):
                        raise RuntimeError('Invalid PR detail')
                    if detail['state'] == 'closed':
                        continue
                    pr['title'] = str(detail.get('title') or pr['title'])[:500]
                    pr['draft'] = detail.get('draft') if isinstance(detail.get('draft'),bool) else None
                    pr['merge'] = 'mergeable' if detail.get('mergeable') is True else 'conflicting' if detail.get('mergeable') is False else 'unknown'
                except Exception:
                    detail = None
                if detail:
                    try:
                        reviews = listing(base + '/pulls/' + str(number) + '/reviews')
                        decisions = {}
                        for review in sorted(reviews,key=lambda r:r.get('id',0)):
                            login = (review.get('user') or {}).get('login')
                            state = review.get('state')
                            if login and state in ('APPROVED','CHANGES_REQUESTED','DISMISSED'):
                                decisions[login] = state
                        states = list(decisions.values())
                        pr['review'] = 'changes_requested' if 'CHANGES_REQUESTED' in states else 'approved' if 'APPROVED' in states else 'pending' if detail.get('requested_reviewers') or detail.get('requested_teams') else 'none'
                    except Exception:
                        pass
                    try:
                        sha = (detail.get('head') or {}).get('sha')
                        if not isinstance(sha,str) or not re.fullmatch(r'[a-fA-F0-9]{40,64}',sha):
                            raise RuntimeError('Invalid commit')
                        runs = listing(base + '/commits/' + sha + '/check-runs?filter=latest','check_runs')
                        status = get(base + '/commits/' + sha + '/status?per_page=10')
                        if not isinstance(status,dict) or not isinstance(status.get('total_count'),int):
                            raise RuntimeError('Invalid commit status')
                        states = []
                        if status['total_count'] > 0:
                            states.append({'success':'passing','failure':'failing','error':'failing','pending':'pending'}.get(status.get('state'),'unknown'))
                        for run in runs:
                            if run.get('status') != 'completed':
                                states.append('pending')
                            elif run.get('conclusion') in ('success','neutral','skipped'):
                                states.append('passing')
                            elif run.get('conclusion') in ('failure','cancelled','timed_out','action_required','startup_failure','stale'):
                                states.append('failing')
                            else:
                                states.append('unknown')
                        pr['checks'] = 'failing' if 'failing' in states else 'unknown' if 'unknown' in states else 'pending' if 'pending' in states else 'passing' if states else 'none'
                    except Exception:
                        pass
                item['pullRequests'].append(pr)
            item['totalOpen'] = len(item['pullRequests'])
            item['status'] = 'ok'
            item['message'] = 'Some status details are unknown.' if any('unknown' in (p['review'],p['checks'],p['merge']) for p in item['pullRequests']) else ''
        except Exception as exc:
            item['message'] = ('GitHub read failed: ' + str(exc))[:300]
            item['pullRequests'] = []
        output.append(item)
    data = {'observedAt':datetime.now(timezone.utc).isoformat(),'expectedIntervalSec':interval,'repositories':output}
    try:
        job.feed.publish('agent.github-pull-requests',data,expected_interval_seconds=interval)
    except Exception as exc:
        return {'published':False,'publication_error':str(exc),'error_type':type(exc).__name__,'data':data}
    return {'published':True,'data':data}
