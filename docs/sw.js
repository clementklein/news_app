const V="veille-v3",COQUILLE=["./","index.html","manifest.webmanifest","icon.svg"];
self.addEventListener("install",e=>e.waitUntil(caches.open(V).then(c=>c.addAll(COQUILLE)).then(()=>self.skipWaiting())));
self.addEventListener("activate",e=>e.waitUntil(caches.keys().then(k=>Promise.all(k.filter(n=>n!==V).map(n=>caches.delete(n)))).then(()=>clients.claim())));
self.addEventListener("fetch",e=>{
  const u=new URL(e.request.url);if(u.origin!==location.origin)return;
  if(u.pathname.endsWith("articles.json")){ // réseau d'abord, cache en secours (mode hors ligne)
    e.respondWith(fetch(e.request).then(r=>{const c=r.clone();caches.open(V).then(x=>x.put(e.request,c));return r}).catch(()=>caches.match(e.request)));
  }else e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));
});
