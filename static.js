(function(){
  document.addEventListener('DOMContentLoaded', function(){
    document.querySelectorAll('.page.fade-in').forEach(function(el){
      el.style.opacity = 0;
      requestAnimationFrame(function(){
        el.style.transition = 'opacity .35s ease';
        el.style.opacity = 1;
      });
    });
  });
  function refreshStock(){
    var el = document.getElementById('stockCount');
    if(!el) return;
    fetch('/api/live-stock', {credentials:'same-origin'})
      .then(function(r){ return r.ok ? r.json() : null; })
      .then(function(d){
        if(!d) return;
        var cur = parseInt(el.textContent, 10) || 0;
        if(d.total !== cur){
          el.classList.add('pulse');
          el.textContent = d.total;
          setTimeout(function(){ el.classList.remove('pulse'); }, 1400);
        }
      }).catch(function(){});
  }
  if(document.getElementById('stockCount')){
    refreshStock();
    setInterval(refreshStock, 15000);
  }
  document.addEventListener('click', function(e){
    if(e.target.closest('.nav-links a')) document.body.classList.remove('nav-open');
  });
})();