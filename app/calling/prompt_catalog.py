"""Read-only owner inventory of application-owned model instructions and templates."""
import ast
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def template(node):
    if isinstance(node,ast.Constant) and isinstance(node.value,str):return node.value
    if isinstance(node,ast.JoinedStr):
        return ''.join(v.value if isinstance(v,ast.Constant) else '{'+ast.unparse(v.value)+'}' for v in node.values)
    if isinstance(node,ast.BinOp) and isinstance(node.op,ast.Add):
        left=template(node.left);right=template(node.right)
        return left+right if left is not None and right is not None else (left or '')+'{runtime expression}'+(right or '')
    if isinstance(node,(ast.Dict,ast.List,ast.Tuple)):
        values=node.values if isinstance(node,ast.Dict) else node.elts
        return '\n\n'.join(text for v in values if v is not None and (text:=template(v)))
    if isinstance(node,ast.Name):return '{'+node.id+'}'
    if isinstance(node,ast.IfExp):
        return '\n[conditional branch]\n'.join(filter(None,(template(node.body),template(node.orelse))))
    if isinstance(node,ast.Call):
        return '\n'.join(text for arg in node.args if (text:=template(arg)))
    return None


def system_prompt_catalog():
    sections=[];seen=set()
    paths=sorted((ROOT/'app').rglob('*.py'))+[ROOT/'scripts/prepare_recording_notices.py',ROOT/'scripts/prepare_owner_prompts.py']
    for path in paths:
        if path.name=='prompt_catalog.py' or not path.is_file():continue
        source=path.read_text();tree=ast.parse(source)
        for node in ast.walk(tree):
            value=None;label=''
            if isinstance(node,(ast.Assign,ast.AnnAssign,ast.AugAssign)):
                targets=node.targets if isinstance(node,ast.Assign) else [node.target]
                names=[t.id for t in targets if isinstance(t,ast.Name)]
                label=', '.join(names)
                if any(any(word in n.casefold() for word in ('prompt','policy','instructions','backend','opening','greeting','hint','note')) for n in names):value=node.value
            elif isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and any(word in node.name for word in ('prompt','policy','identity')):
                returns=[template(n.value) for n in ast.walk(node) if isinstance(n,ast.Return)]
                for entry in filter(None,returns):
                    if len(entry)>60 and entry not in seen:
                        seen.add(entry);sections.append({'title':node.name,'source':str(path.relative_to(ROOT)),
                            'line':node.lineno,'text':entry,'kind':'template'})
            elif isinstance(node,ast.keyword) and node.arg in ('instruction','instructions','system_instruction'):
                label=node.arg;value=node.value
            elif isinstance(node,ast.Dict):
                for key,val in zip(node.keys,node.values):
                    if isinstance(key,ast.Constant) and key.value in ('instruction','instructions','system_instruction'):
                        text=template(val)
                        if text and len(text)>60 and text not in seen:
                            seen.add(text);sections.append({'title':'Session instructions template','source':str(path.relative_to(ROOT)),
                                'line':val.lineno,'text':text,'kind':'template'})
            text=template(value) if value else None
            if text and len(text)>60 and text not in seen:
                seen.add(text);sections.append({'title':label or 'Instructions','source':str(path.relative_to(ROOT)),
                    'line':value.lineno,'text':text,'kind':'template' if '{' in text else 'literal'})
    return {'sections':sections,'scope':'Application-owned prompts, including calendar intent/name matching, history/Luna, '
            'voice/planner, personality/presentation and private owner coaching. Templates show runtime placeholders; '
            'use the assembled preview for effective call settings. Provider-internal instructions are not exposed by provider APIs.',
            'read_only':True,'provider_requests':False}
