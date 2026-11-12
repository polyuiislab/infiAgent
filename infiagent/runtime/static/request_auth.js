(() => {
  const token=()=>sessionStorage.getItem('infiagent-runtime-token')||'';
  const applies=input=>{try{const u=new URL(typeof input==='string'?input:input.url,location.href);return u.origin===location.origin&&u.pathname.startsWith('/api/');}catch{return false;}};
  const original=window.fetch.bind(window);
  window.fetch=(input,options={})=>{if(token()&&applies(input)){const headers=new Headers(options.headers||(input instanceof Request?input.headers:undefined));headers.set('Authorization','Bearer '+token());options={...options,headers};}return original(input,options);};
  const open=XMLHttpRequest.prototype.open,send=XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open=function(method,url,...rest){this._infiagentUrl=url;return open.call(this,method,url,...rest);};
  XMLHttpRequest.prototype.send=function(body){if(token()&&applies(this._infiagentUrl))this.setRequestHeader('Authorization','Bearer '+token());return send.call(this,body);};
  document.addEventListener('DOMContentLoaded',()=>{const host=document.querySelector('.runtime-card');if(!host)return;const section=document.createElement('details');const summary=document.createElement('summary');summary.textContent='运行时访问令牌（可选）';const field=document.createElement('input');field.type='password';field.autocomplete='off';field.placeholder='仅保存在当前标签页';field.value=token();const save=document.createElement('button');save.className='secondary';save.textContent='应用并刷新';save.onclick=()=>{if(field.value)sessionStorage.setItem('infiagent-runtime-token',field.value);else sessionStorage.removeItem('infiagent-runtime-token');location.reload();};section.append(summary,field,save);host.append(section);});
})();
