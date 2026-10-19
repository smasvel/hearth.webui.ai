"""Live MiniCPM tool-call smoke tests; tool calls are inspected, never executed."""
import json

import requests
URL='http://127.0.0.1:52626/v1/chat/completions'
T={'type':'function','function':{'name':'get_weather','description':'Get weather for a city','parameters':{'type':'object','properties':{'city':{'type':'string'}},'required':['city']}}}
def run(name,prompt,stream=False,**kw):
    b=dict(model='minicpm-v-4.7:1b',messages=[{'role':'user','content':prompt}],tools=[T],temperature=0,max_tokens=512,stream=stream);b.update(kw)
    try:
        r=requests.post(URL,json=b,timeout=120);r.raise_for_status()
        if stream:
            events=[json.loads(x[6:]) for x in r.text.splitlines() if x.startswith('data: ') and x[6:]!='[DONE]']; data=events
        else: data=[r.json()]
        calls={}
        finishes=[]
        for event in data:
            for choice in event.get('choices',[]):
                message=choice.get('delta',choice.get('message',{}))
                if choice.get('finish_reason'): finishes.append(choice['finish_reason'])
                for call in message.get('tool_calls',[]):
                    item=calls.setdefault(call.get('index',0),{'name':'','arguments':''})
                    fn=call.get('function',{})
                    item['name']+=fn.get('name','')
                    item['arguments']+=fn.get('arguments','')
        expected=2 if name.startswith('parallel') else 0 if name.startswith(('none','no-tool-needed','result')) else 1
        assert len(calls)==expected, (name,calls)
        assert finishes[-1]==('tool_calls' if expected else 'stop'), (name,finishes)
        cities=[]
        for call in calls.values():
            assert call['name']=='get_weather',call
            cities.append(json.loads(call['arguments'])['city'])
        if expected: assert sorted(cities)==(['Boston','Paris'] if expected==2 else ['Boston']), cities
        if stream: assert 'data: [DONE]' in r.text
        print('PASS',name,flush=True)
    except Exception:
        print('FAIL',name,flush=True)
        raise
if __name__ == "__main__":
    for s in [False,True]:
        run('auto '+str(s),'Use get_weather for Boston.',s)
        run('none '+str(s),'Use get_weather for Boston.',s,tool_choice='none')
        run('required '+str(s),'Boston weather?',s,tool_choice='required')
        run('forced '+str(s),'Boston weather?',s,tool_choice={'type':'function','function':{'name':'get_weather'}})
        run('no-tool-needed '+str(s),'What is 2+2? Answer directly.',s)
        run('parallel '+str(s),'Get weather for both Boston and Paris. Call get_weather for each.',s,parallel_tool_calls=True)
        run('result '+str(s),'',s,messages=[{'role':'user','content':'Weather in Boston?'},{'role':'assistant','content':None,'tool_calls':[{'id':'c1','type':'function','function':{'name':'get_weather','arguments':'{"city":"Boston"}'}}]},{'role':'tool','tool_call_id':'c1','content':'{"temperature":21,"conditions":"sunny"}'}])
