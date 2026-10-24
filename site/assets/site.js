// Сцени з ролика: підписи проявляються, відео грають лише коли сцена на екрані (економія батареї й трафіку).
(function(){
  var reduce=window.matchMedia&&matchMedia("(prefers-reduced-motion: reduce)").matches;
  function load(v){if(!v.src&&v.dataset.src){v.src=v.dataset.src}}
  var scenes=[].slice.call(document.querySelectorAll(".scene"));
  if("IntersectionObserver" in window){
    var io=new IntersectionObserver(function(es){es.forEach(function(e){
      var vids=e.target.querySelectorAll("video");
      if(e.isIntersecting){e.target.classList.add("on");
        if(!reduce)vids.forEach(function(v){load(v);var p=v.play();if(p&&p.catch)p.catch(function(){})})}
      else vids.forEach(function(v){v.pause()})})},{threshold:[0,.3]});
    scenes.forEach(function(s){io.observe(s)});
  }else scenes.forEach(function(s){s.classList.add("on")});
  // тема: ранок / ніч (запам'ятовується)
  var tb=document.querySelector(".theme");
  if(tb)tb.addEventListener("click",function(){var r=document.documentElement;
    var dark=r.dataset.theme?r.dataset.theme==="night":matchMedia("(prefers-color-scheme: dark)").matches;
    r.dataset.theme=dark?"morning":"night";try{localStorage.setItem("cb_theme",r.dataset.theme)}catch(e){}});
  // меню на телефоні
  var bg=document.querySelector(".burger");
  if(bg)bg.addEventListener("click",function(){var o=document.body.classList.toggle("nav-open");bg.setAttribute("aria-expanded",o)});
  document.querySelectorAll("#nav a").forEach(function(a){a.addEventListener("click",function(){document.body.classList.remove("nav-open")})});
})();
