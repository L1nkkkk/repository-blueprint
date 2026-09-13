const test=require('node:test');
const assert=require('node:assert/strict');
const Panels=require('../codemap/web/panel-layout.js');
const Highlight=require('../codemap/web/source-highlight.js');

test('resizing and collapsed preferences reserve canvas space without rewriting saved widths',()=>{
  const saved={files:{width:420,collapsed:false},details:{width:850,collapsed:false}};
  const before=structuredClone(saved),small=Panels.measure(saved,1000);
  assert.ok(1000-small.files.width-small.details.width-12>=279);
  assert.ok(small.files.width>=180&&small.details.width>=260);
  assert.deepEqual(saved,before);
  const wide=Panels.measure(saved,1800);assert.equal(wide.files.width,420);assert.equal(wide.details.width,850);
  const hidden=Panels.measure({...saved,files:{width:420,collapsed:true}},1200);
  assert.equal(hidden.files.open,false);assert.ok(hidden.details.width>small.details.width);
});

test('narrow windows use one drawer and reopening desktop restores its own panel state',()=>{
  const saved={files:{width:300,collapsed:true},details:{width:600,collapsed:false}};
  const mobile=Panels.measure(saved,390,'files');
  assert.equal(mobile.files.open,true);assert.equal(mobile.files.overlay,true);
  assert.equal(mobile.details.open,false);assert.ok(mobile.files.width<=390*.9);
  const tablet=Panels.measure(saved,800,'details');assert.equal(tablet.files.open,false);assert.equal(tablet.details.open,true);
  const desktop=Panels.measure(saved,1500);assert.equal(desktop.files.open,false);assert.equal(desktop.details.open,true);assert.equal(desktop.details.width,600);
  assert.deepEqual(Panels.normalize({files:{width:NaN},details:{width:-100}}),{files:{width:234,collapsed:false},details:{width:260,collapsed:false}});
});

function render(language,text,start=1,end){
  const lines=text.split('\n');end??=lines.length;
  return Highlight.render({language,path:'example',start_line:start,highlight_prefix:lines.slice(0,start-1).map(line=>line+'\n').join(''),lines:lines.slice(start-1,end).map((text,i)=>({number:start+i,text}))});
}
function plain(html){return html.replace(/<\/?span\b[^>]*>/g,'').replace(/&(amp|lt|gt|quot|#x27);/g,(_,name)=>({amp:'&',lt:'<',gt:'>',quot:'"','#x27':"'"})[name]);}

test('the five requested languages have colored syntax and retain exact selectable text',()=>{
  const fixtures={
    cpp:'#include <string>\n// 中文注释\nint main() { return 42; }',
    python:'def greet(name):\n    # 注释\n    return "你好" + name',
    csharp:'public class Motion {\n    public string Name = "玩家";\n}',
    typescript:'interface State { count: number }\nconst value: State = { count: 2 };',
    javascript:'// source code, not executable HTML\nconst text = "<img src=x onerror=alert(1)> & </script>";'
  };
  for(const [language,text] of Object.entries(fixtures)){
    const result=render(language,text);assert.equal(result.language,language);
    assert.ok(result.lines.some(line=>line.includes('hljs-keyword')),language);
    assert.deepEqual(result.lines.map(plain),text.split('\n'),language);
    assert.ok(!result.lines.join('').includes('<img'));
  }
});

test('excerpt boundaries retain multiline strings and comments, then return to code colors',()=>{
  for(const [language,text,kind] of [
    ['python','doc = """start\n    return is text\nend"""\nreturn 4','string'],
    ['cpp','/* start\nreturn is a comment\nend */\nint result = 4;','comment']
  ]){
    const result=render(language,text,2);
    assert.match(result.lines[0],new RegExp('hljs-'+kind));
    assert.ok(!result.lines[0].includes('hljs-keyword'));
    assert.ok(result.lines[2].includes('hljs-keyword')||result.lines[2].includes('hljs-type'));
    assert.deepEqual(result.lines.map(plain),text.split('\n').slice(1));
    for(const line of result.lines)assert.equal((line.match(/<span /g)||[]).length,(line.match(/<\/span>/g)||[]).length);
  }
  assert.deepEqual(render('unknown-language','<script>bad()</script>').lines,[]);
});
