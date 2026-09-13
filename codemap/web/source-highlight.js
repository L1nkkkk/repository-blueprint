/* Local worker: highlight verified text without blocking canvas interactions. */
(function(factory){
  if(typeof module==='object'&&module.exports)module.exports=factory(require('./vendor/highlight.min.js'));
  else {importScripts('/vendor/highlight.min.js');const api=factory(self.hljs);self.onmessage=event=>self.postMessage(api.render(event.data));}
})(function(hljs){
  'use strict';
  const aliases={'c#':'csharp','c++':'cpp',shell:'bash'};
  const extensions={json:'json',uproject:'json',uplugin:'json',html:'xml',htm:'xml',xml:'xml',csproj:'xml',props:'xml',targets:'xml',css:'css',md:'markdown',yaml:'yaml',yml:'yaml',toml:'ini',ini:'ini',cfg:'ini',cmake:'cmake'};
  function languageFor(language,path){
    let candidate=aliases[language]||language||extensions[path.split('.').pop().toLowerCase()];
    if(path.toLowerCase().endsWith('cmakelists.txt'))candidate='cmake';
    return candidate&&hljs.getLanguage(candidate)?candidate:null;
  }
  // Close and reopen spans at line boundaries, including spans opened before the excerpt.
  function excerpt(html,start,count){
    const stack=[],result=[];let line=1,part='',cursor=0;
    for(const match of html.matchAll(/<span class="[\w ._-]+">|<\/span>|\n/g)){
      part+=html.slice(cursor,match.index);cursor=match.index+match[0].length;
      if(match[0]==='\n'){
        if(line>=start)result.push(part+'</span>'.repeat(stack.length));
        if(result.length===count)return result;
        line++;part=stack.join('');
      }else{
        part+=match[0];if(match[0]==='</span>')stack.pop();else stack.push(match[0]);
      }
    }
    if(line>=start&&result.length<count)result.push(part+html.slice(cursor)+'</span>'.repeat(stack.length));
    return result;
  }
  function render(data){
    const language=languageFor(data.language,data.path);
    if(!language)return {language:null,lines:[]};
    const code=(data.highlight_prefix||'')+data.lines.map(line=>line.text).join('\n');
    const html=hljs.highlight(code,{language,ignoreIllegals:true}).value;
    return {language,lines:excerpt(html,data.start_line,data.lines.length)};
  }
  return {render,languageFor};
});
