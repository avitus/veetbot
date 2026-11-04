from datetime import datetime, timezone
import re

STATES = ['passing','failing','pending','skipped','none','unknown']
REVIEW_STATES = ['approved','changes_requested','review_required','pending','commented','none','unknown']
PR_PROPERTIES = {
    'number': {'type':'integer','minimum':1},
    'title': {'type':'string','maxLength':500},
    'author': {'type':'string','maxLength':100},
    'draft': {'type':['boolean','null']},
    'review': {'enum':REVIEW_STATES},
    'checks': {'enum':STATES},
    'merge': {'enum':['mergeable','conflicting','unknown']},
    'automatedReview': {'enum':STATES},
    'automatedReviewNames': {'type':'string','maxLength':200},
    'security': {'enum':STATES},
    'mergeState': {'enum':['clean','blocked','behind','dirty','draft','unstable','unknown','has_hooks']},
    'reviewOnOlderCommit': {'type':'boolean'},
    'classificationVersion': {'const':2}
}
SCHEMA = {
    'type':'object','additionalProperties':False,
    'required':['observedAt','expectedIntervalSec','repositories'],
    'properties':{
        'observedAt':{'type':'string','format':'date-time'},
        'expectedIntervalSec':{'type':'integer','minimum':1},
        'repositories':{'type':'array','maxItems':10,'items':{
            'type':'object','additionalProperties':False,
            'required':['name','status','message','totalOpen','pullRequests'],
            'properties':{
                'name':{'type':'string','maxLength':200},
                'status':{'enum':['ok','unavailable']},
                'message':{'type':'string','maxLength':300},
                'totalOpen':{'type':['integer','null'],'minimum':0},
                'pullRequests':{'type':'array','maxItems':300,'items':{
                    'type':'object','additionalProperties':False,
                    'required':['number','title','author','draft','review','checks','merge'],
                    'properties':PR_PROPERTIES
                }}
            }
        }}
    }
}

def category(name, app=''):
    name = str(name or '').lower()
    app = str(app or '').lower()
    if app == 'coderabbitai' or re.match(r'^coderabbit(?:ai)?(?:$|[ /:_-])', name):
        return 'review'
    if app == 'gitguardian' or re.match(r'^gitguardian(?:$|[ /:_-])', name):
        return 'security'
    return 'ci'

def check_state(run):
    if run.get('status') != 'completed':
        return 'pending' if run.get('status') in ('queued','in_progress','waiting','pending','requested') else 'unknown'
    result = run.get('conclusion')
    if result == 'success':
        return 'passing'
    if result in ('neutral','skipped'):
        return 'skipped'
    if result in ('failure','cancelled','timed_out','action_required','startup_failure','stale'):
        return 'failing'
    return 'unknown'

def aggregate(values, complete=True):
    if 'failing' in values:
        return 'failing'
    if not complete or 'unknown' in values:
        return 'unknown'
    if 'pending' in values:
        return 'pending'
    if 'passing' in values:
        return 'passing'
    return 'skipped' if values else 'none'

def classify_checks(runs, statuses, complete=True):
    groups = {'review':[], 'security':[], 'ci':[]}
    names = set()
    latest_runs = {}
    for run in runs:
        app = run.get('app') or {}
        key = (app.get('id',app.get('slug','')), run.get('name',''))
        if run.get('id',0) >= latest_runs.get(key,{}).get('id',-1):
            latest_runs[key] = run
    for run in latest_runs.values():
        group = category(run.get('name'), (run.get('app') or {}).get('slug'))
        groups[group].append(check_state(run))
        if group == 'review':
            names.add(str(run.get('name') or 'Automated review'))
    latest_statuses = {}
    for status in statuses:
        context = status.get('context','')
        if status.get('id',0) >= latest_statuses.get(context,{}).get('id',-1):
            latest_statuses[context] = status
    for context, status in latest_statuses.items():
        group = category(context)
        groups[group].append({'success':'passing','failure':'failing','error':'failing','pending':'pending'}.get(status.get('state'),'unknown'))
        if group == 'review':
            names.add(str(context))
    return {
        'checks':aggregate(groups['ci'],complete),
        'security':aggregate(groups['security'],complete),
        'automatedReview':aggregate(groups['review'],complete),
        'automatedReviewNames':', '.join(sorted(names))[:200]
    }

def classify_reviews(entries, detail):
    latest = {}
    for review in sorted(entries,key=lambda r:r.get('id',0)):
        login = (review.get('user') or {}).get('login')
        if login and review.get('state') in ('APPROVED','CHANGES_REQUESTED','DISMISSED'):
            latest[login] = review
    states = [r.get('state') for r in latest.values()]
    state = 'changes_requested' if 'CHANGES_REQUESTED' in states else 'approved' if 'APPROVED' in states else 'pending' if detail.get('requested_reviewers') or detail.get('requested_teams') else 'commented' if any(r.get('state') == 'COMMENTED' for r in entries) else 'none'
    sha = (detail.get('head') or {}).get('sha')
    older = any(r.get('state') in ('APPROVED','CHANGES_REQUESTED') and r.get('commit_id') and r['commit_id'] != sha for r in latest.values())
    return {'review':state,'reviewOnOlderCommit':older}

def self_test():
    def run(name, app, state, conclusion, ident=1):
        return {'id':ident,'name':name,'app':{'slug':app},'status':state,'conclusion':conclusion}
    sample = classify_checks([run('CodeRabbit','coderabbitai','in_progress',None),run('GitGuardian Security Checks','gitguardian','completed','success')],[])
    assert sample['automatedReview'] == 'pending' and sample['checks'] == 'none' and sample['security'] == 'passing'
    assert classify_checks([run('CodeRabbit','coderabbitai','completed','success')],[])['checks'] == 'none'
    assert classify_checks([run('Tests','github-actions','completed','failure')],[])['checks'] == 'failing'
    assert classify_checks([run('Tests','github-actions','completed','skipped')],[])['checks'] == 'skipped'
    assert classify_checks([],[],False)['checks'] == 'unknown'
    assert classify_checks([], [{'id':1,'context':'CodeRabbit','state':'pending'}])['checks'] == 'none'
    assert classify_checks([], [{'id':1,'context':'build','state':'failure'},{'id':2,'context':'build','state':'success'}])['checks'] == 'passing'
    assert classify_checks([run('Tests','github-actions','completed','failure',1),run('Tests','github-actions','completed','success',2)],[])['checks'] == 'passing'
    detail={'head':{'sha':'new'}}
    entries=[{'id':1,'user':{'login':'reviewer'},'state':'APPROVED','commit_id':'old'},{'id':2,'user':{'login':'reviewer'},'state':'COMMENTED'}]
    assert classify_reviews(entries,detail) == {'review':'approved','reviewOnOlderCommit':True}
    entries.append({'id':3,'user':{'login':'reviewer'},'state':'DISMISSED'})
    assert classify_reviews(entries,detail)['review'] == 'commented'
    return 10

# A reply over the broker's response limit ends the whole run, even if caught, so
# each query's largest possible reply must fit: at most about 32 KB for a full list
# page and about 46 KB for one pull request (measured with the longest values).
LIST_QUERY = '''
query OpenPullRequests($owner: String!, $name: String!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: 10, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes { number title isDraft author { login } }
    }
  }
}
'''
DETAIL_QUERY = '''
query PullRequestStatus($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      number state title isDraft mergeable mergeStateStatus headRefOid
      reviewRequests(first: 1) { totalCount }
      reviews(first: 100) {
        pageInfo { hasNextPage }
        nodes { databaseId state commit { oid } author { login } }
      }
      commits(last: 1) {
        nodes {
          commit {
            oid
            statusCheckRollup {
              contexts(first: 50) {
                pageInfo { hasNextPage }
                nodes {
                  __typename
                  ... on CheckRun { databaseId name status conclusion checkSuite { app { databaseId slug } } }
                  ... on StatusContext { context state }
                }
              }
            }
          }
        }
      }
    }
  }
}
'''

def rest_review(node):
    """Give a GraphQL review the shape classify_reviews reads."""
    return {'id':node.get('databaseId') or 0,'user':node.get('author'),'state':node.get('state'),'commit_id':(node.get('commit') or {}).get('oid')}

def rest_run(node):
    """Give a GraphQL check run the shape classify_checks reads."""
    app=(node.get('checkSuite') or {}).get('app') or {}
    return {'id':node.get('databaseId') or 0,'name':node.get('name'),'app':{'id':app.get('databaseId'),'slug':app.get('slug')},'status':str(node.get('status') or '').lower(),'conclusion':str(node.get('conclusion') or '').lower() or None}

def rest_status(node):
    """Give a GraphQL commit status the shape classify_checks reads."""
    state=str(node.get('state') or '').lower()
    return {'context':node.get('context',''),'state':'pending' if state == 'expected' else state}

def main(inputs, job):
    self_test()
    repos = inputs['repositories']
    interval = inputs['refresh_seconds']
    if not 1 <= len(repos) <= 10 or len(set(repos)) != len(repos) or not 60 <= interval <= 86400:
        raise ValueError('Invalid inputs')
    for repo in repos:
        if not re.fullmatch(r'[A-Za-z0-9-]+/[A-Za-z0-9_.-]+',repo) or repo.split('/')[1] in ('.','..'):
            raise ValueError('Invalid repository')
    used = [0]
    def graphql(query, variables):
        if used[0] >= 45:
            raise RuntimeError('Request budget exhausted')
        used[0] += 1
        response = job.http.request('POST','https://api.github.com/graphql',json={'query':query,'variables':variables})
        if response.get('status') != 200:
            raise RuntimeError('GitHub HTTP ' + str(response.get('status')))
        body = response.get('json')
        if not isinstance(body,dict):
            raise RuntimeError('Unexpected GraphQL response')
        if body.get('errors'):
            kinds = sorted({str(error.get('type') or 'error')[:40] for error in body['errors'] if isinstance(error,dict)})
            raise RuntimeError('GitHub GraphQL ' + ','.join(kinds))
        repository = (body.get('data') or {}).get('repository')
        if not isinstance(repository,dict):
            raise RuntimeError('Repository unavailable')
        return repository
    def open_pulls(owner, name):
        items=[]
        cursors=set()
        after=None
        for _ in range(30):
            connection=graphql(LIST_QUERY,{'owner':owner,'name':name,'after':after}).get('pullRequests')
            if not isinstance(connection,dict) or not isinstance(connection.get('nodes'),list):
                raise RuntimeError('Unexpected list response')
            items.extend(connection['nodes'])
            info=connection.get('pageInfo') or {}
            if not info.get('hasNextPage'):
                if connection.get('totalCount',len(items)) > len(items):
                    raise RuntimeError('Incomplete list')
                return items
            after=info.get('endCursor')
            if not after or after in cursors:
                raise RuntimeError('Invalid list cursor')
            cursors.add(after)
        raise RuntimeError('Pagination limit reached')
    output=[]
    for repo in repos:
        owner,name=repo.split('/')
        item={'name':repo,'status':'unavailable','message':'','totalOpen':None,'pullRequests':[]}
        problems=set()
        try:
            pulls=open_pulls(owner,name)
            seen=set()
            for pull in pulls:
                number=pull.get('number') if isinstance(pull,dict) else None
                if not isinstance(number,int):
                    raise RuntimeError('Invalid open PR list')
                if number in seen:
                    continue
                seen.add(number)
                pr={'number':number,'title':str(pull.get('title') or '')[:500],'author':str((pull.get('author') or {}).get('login') or '')[:100],'draft':pull.get('isDraft') if isinstance(pull.get('isDraft'),bool) else None,'review':'unknown','checks':'unknown','merge':'unknown','automatedReview':'unknown','automatedReviewNames':'','security':'unknown','mergeState':'unknown','reviewOnOlderCommit':False,'classificationVersion':2}
                detail=None
                try:
                    detail=graphql(DETAIL_QUERY,{'owner':owner,'name':name,'number':number}).get('pullRequest')
                    if not isinstance(detail,dict) or detail.get('number') != number or detail.get('state') not in ('OPEN','CLOSED','MERGED'):
                        raise RuntimeError('Invalid PR detail')
                    if detail['state'] != 'OPEN':
                        continue
                    pr['title']=str(detail.get('title') or pr['title'])[:500]
                    pr['draft']=detail.get('isDraft') if isinstance(detail.get('isDraft'),bool) else None
                    pr['merge']={'MERGEABLE':'mergeable','CONFLICTING':'conflicting'}.get(detail.get('mergeable'),'unknown')
                    merge_state=str(detail.get('mergeStateStatus') or '').lower()
                    pr['mergeState']=merge_state if merge_state in PR_PROPERTIES['mergeState']['enum'] else 'unknown'
                except Exception as exc:
                    detail=None
                    problems.add('PR detail: '+str(exc))
                if detail:
                    sha=detail.get('headRefOid')
                    reviews=detail.get('reviews') or {}
                    if isinstance(reviews.get('nodes'),list) and not (reviews.get('pageInfo') or {}).get('hasNextPage'):
                        requests=(detail.get('reviewRequests') or {}).get('totalCount')
                        pr.update(classify_reviews([rest_review(node) for node in reviews['nodes']],{'head':{'sha':sha},'requested_reviewers':requests}))
                    else:
                        problems.add('Reviews: more than 100')
                    commits=(detail.get('commits') or {}).get('nodes') or []
                    commit=(commits[-1] or {}).get('commit') or {} if commits else {}
                    if isinstance(sha,str) and commit.get('oid') == sha:
                        contexts=(commit.get('statusCheckRollup') or {}).get('contexts') or {}
                        nodes=contexts.get('nodes') or []
                        complete=not (contexts.get('pageInfo') or {}).get('hasNextPage')
                        if not complete:
                            problems.add('Check runs: more than 50')
                        runs=[rest_run(node) for node in nodes if node.get('__typename') == 'CheckRun']
                        statuses=[rest_status(node) for node in nodes if node.get('__typename') == 'StatusContext']
                        pr.update(classify_checks(runs,statuses,complete))
                    else:
                        problems.add('Head commit unavailable')
                item['pullRequests'].append(pr)
            item['totalOpen']=len(item['pullRequests'])
            item['status']='ok'
            item['message']='; '.join(sorted(problems))[:300]
        except Exception as exc:
            item['message']=('GitHub read failed: '+str(exc))[:300]
            item['pullRequests']=[]
        output.append(item)
    data={'observedAt':datetime.now(timezone.utc).isoformat(),'expectedIntervalSec':interval,'repositories':output}
    job.feed.publish('agent.github-pull-requests',data,expected_interval_seconds=interval,schema=SCHEMA)
    return {'published':True,'classification_tests':10,'requests':used[0],'data':data}
