'use strict';
// Password forms use normal navigation so password managers can detect sign-in.
document.getElementById('pairing-login').addEventListener('submit', async event => {
  event.preventDefault();
  const button=event.currentTarget.querySelector('button'); button.disabled=true;
  try {
    const response=await fetch('/live/login',{method:'POST',credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({code:document.getElementById('code').value})});
    const result=await response.json();
    if (!response.ok) throw new Error(result.detail || 'Pairing failed');
    location.replace('/live');
  } catch(error) { document.getElementById('login-error').textContent=error.message; button.disabled=false; }
});
