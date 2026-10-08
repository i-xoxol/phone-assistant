'use strict';
document.querySelector('form').addEventListener('submit', async event => {
  event.preventDefault();
  const button=document.querySelector('button'); button.disabled=true;
  try {
    const response=await fetch('/live/login',{method:'POST',credentials:'same-origin',
      headers:{'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},
      body:new URLSearchParams({code:document.querySelector('input').value})});
    const result=await response.json();
    if (!response.ok) throw new Error(result.detail || 'Pairing failed');
    location.replace('/live');
  } catch(error) { document.getElementById('login-error').textContent=error.message; button.disabled=false; }
});
