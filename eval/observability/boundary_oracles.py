"""Trusted parent checks for archive, rebuild and configuration boundaries."""
from __future__ import annotations

import textwrap

CHILD = """import importlib,json,sys
sys.path.insert(0,'/source')
module,name,args=json.load(sys.stdin)
try:
    result=getattr(importlib.import_module(module),name)(*args)
except ValueError:
    response={'kind':'ValueError'}
except BaseException:
    response={'kind':'execution_error'}
else:
    response={'kind':'returned','value':result}
print(json.dumps(response))
"""

HEADER = """import hashlib,json,os,pathlib,subprocess,sys,tempfile,zipfile
data=json.load(sys.stdin)
def call(module,name,*args):
    try:
        child=subprocess.run([sys.executable,'-I','-c',__CHILD__],input=json.dumps([module,name,args]),text=True,capture_output=True,timeout=5,user=65534,group=65534,extra_groups=[])
        value=json.loads(child.stdout)
        assert child.returncode==0
        return value
    except (ValueError,subprocess.SubprocessError,AssertionError):
        return {'kind':'execution_error'}
def returned(module,name,*args):
    value=call(module,name,*args)
    assert value['kind']=='returned',value
    return value.get('value')
def folder():
    path=pathlib.Path(tempfile.mkdtemp(dir='/work'));os.chmod(path,0o777);return path
def sha(value):return hashlib.sha256(value).hexdigest()
""".replace("__CHILD__", repr(CHILD))

ARCHIVE = """
root=folder();source=root/'source';source.mkdir();archive=root/'bundle.zip';manifest=root/'manifest.json'
expected={'a.txt':b'alpha\\n','nested/b.txt':'中文内容'.encode(),'empty.txt':b''}
for name,content in expected.items():
    path=source/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(content)
returned('archive','build_archive',str(source),str(archive),str(manifest))
with zipfile.ZipFile(archive) as z:
    assert sorted(z.namelist())==sorted(expected)
    assert {name:z.read(name) for name in z.namelist()}==expected
assert json.loads(manifest.read_text())=={name:sha(content) for name,content in sorted(expected.items())}
assert returned('archive','verify_archive',str(archive),str(manifest)) is True
archive.unlink()
with zipfile.ZipFile(archive,'w') as z:
    for name,content in expected.items():z.writestr(name,content+b'tampered')
assert returned('archive','verify_archive',str(archive),str(manifest)) is False
assert all((source/name).read_bytes()==content for name,content in expected.items())
"""

INCREMENTAL = """
root=folder();(root/'src').mkdir();deps={'app':['lib'],'lib':['core'],'core':[]}
for name in deps:(root/'src'/f'{name}.txt').write_text(name+'-v1')
def expected():
    result={}
    for name in ('core','lib','app'):
        result[name]=sha(((root/'src'/f'{name}.txt').read_text()+''.join(result[d] for d in sorted(deps[name]))).encode())
    return result
def outputs():
    return {name:(root/'build'/f'{name}.txt').read_text().strip() for name in deps}
def times():return {name:(root/'build'/f'{name}.txt').stat().st_mtime_ns for name in deps}
assert returned('incremental','build',str(root),deps)==['core','lib','app']
assert outputs()==expected();before=times()
assert returned('incremental','build',str(root),deps)==[]
assert times()==before
(root/'src'/'core.txt').write_text('core-v2')
assert returned('incremental','build',str(root),deps)==['core','lib','app']
assert outputs()==expected();before=times()
(root/'src'/'app.txt').write_text('app-v2')
assert returned('incremental','build',str(root),deps)==['app']
assert outputs()==expected();after=times()
assert after['core']==before['core'] and after['lib']==before['lib']
"""

CONFIGURATION = """
base={'schema_version':3,'timeout':30,'tls':True}
targets=[{'name':'west','enabled':True,'override':{'timeout':10}}, {'name':'east','enabled':False}, {'name':'north','enabled':True}]
actual=returned('configmerge','resolve',base,targets)
assert actual=={'north':base,'west':{'schema_version':3,'timeout':10,'tls':True}}
changed={**base,'schema_version':4,'timeout':50}
actual=returned('configmerge','resolve',changed,targets)
assert actual=={'north':changed,'west':{'schema_version':4,'timeout':10,'tls':True}}
for override in ({'schema_version':2},{'tls':False},{'timeout':0}):
    assert call('configmerge','resolve',base,[{'name':'bad','enabled':True,'override':override}])['kind']=='ValueError'
assert call('configmerge','resolve',base,[{'name':'same','enabled':True},{'name':'same','enabled':True}])['kind']=='ValueError'
"""


def program(task_id: str) -> str | None:
    body = {"holdout.02": ARCHIVE, "holdout.03": INCREMENTAL, "holdout.04": CONFIGURATION}.get(task_id)
    if body is None:
        return None
    return HEADER + "\ntry:\n" + textwrap.indent(body, "    ") + "\nexcept Exception:\n    passed=False\nelse:\n    passed=True\nprint(json.dumps({'probes':[passed]}))\n"
