// Чат «Запитати ШІ»: історія лише в пам'яті сторінки (до 6 останніх реплік іде на сервер для контексту).
(function(){
  const log=document.getElementById("log"),f=document.getElementById("ask"),q=document.getElementById("q"),
        btn=document.getElementById("send"),note=document.getElementById("note");
  const hist=[];
  function add(cls,text){const d=document.createElement("div");d.className="msg "+cls;d.textContent=text;log.append(d);d.scrollIntoView({block:"end",behavior:"smooth"});return d}
  add("ai","Привіт! Я відповідаю на питання про ETH-Агента: як встановити, налаштувати, як працюють стратегії й що показали бектести. Що цікавить?");
  f.addEventListener("submit",async e=>{e.preventDefault();const text=q.value.trim();if(!text)return;
    add("me",text);q.value="";btn.disabled=true;const wait=add("ai","");wait.classList.add("dots");
    try{const r=await fetch("/api/ask",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({q:text,history:hist.slice(-6)})});
      const j=await r.json().catch(()=>({error:"сервер недоступний"}));wait.classList.remove("dots");
      if(!r.ok||j.error){wait.classList.add("err");wait.textContent=j.error||("помилка "+r.status)}
      else{wait.textContent=j.answer;hist.push({role:"user",content:text},{role:"assistant",content:j.answer});
        if(j.left!=null)note.textContent=`Залишилось питань на сьогодні: ${j.left}`}}
    catch(err){wait.classList.remove("dots");wait.classList.add("err");wait.textContent="Немає зв'язку з сервером. Спробуй пізніше."}
    btn.disabled=false;q.focus()});
  q.addEventListener("keydown",e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();f.requestSubmit()}});
})();
