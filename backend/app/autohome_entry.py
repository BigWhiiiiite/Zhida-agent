"""Read visible Autohome recruitment account evidence without opening a menu."""
from __future__ import annotations

from urllib.parse import urlparse

from playwright.async_api import Page


async def inspect_autohome_account(page: Page) -> bool:
    """Recognize the official logged-in header, returning no account details.

    The public main.js populates this exact dropdown only when isLogin is true.
    Its logout menu is hidden until hover, so the visible nickname and the
    logout structure must be checked together. Cookies and nickname text alone
    are not authentication evidence. Active login dialogs remain the caller's
    responsibility and must override a background header.
    """
    parsed = urlparse(page.url)
    if parsed.scheme != "https" or parsed.netloc.casefold() not in {
        "talent.autohome.com.cn", "talent.autohome.com.cn:443"
    }:
        return False
    return bool(await page.evaluate(r"""() => {
      // Recheck after the Python URL read in case the page navigated meanwhile.
      if(location.origin!=='https://talent.autohome.com.cn') return false;
      const clean=value=>String(value||'').replace(/\s+/g,' ').trim();
      const visible=el=>{
        if(!el||!el.getClientRects().length) return false;
        const rect=el.getBoundingClientRect();
        if(rect.width<=0||rect.height<=0) return false;
        for(let node=el;node;node=node.parentElement){
          const style=getComputedStyle(node);
          if(node.hidden||node.getAttribute('aria-hidden')==='true'||
              style.display==='none'||['hidden','collapse'].includes(style.visibility)||
              style.opacity==='0') return false;
        }
        return true;
      };
      const boxes=[...document.querySelectorAll('#header .login-box-n')];
      // A rendered login control conflicts with any stale account dropdown.
      if(boxes.some(box=>[...box.querySelectorAll('a')].some(el=>visible(el)&&
          /^login\s*\(\s*\)\s*;?$/.test(el.getAttribute('onclick')||'')&&
          clean(el.innerText)==='登录'))) return false;
      const accounts=[];
      for(const box of boxes){
        for(const dropdown of box.querySelectorAll(':scope > .dropdown')){
          const names=[...dropdown.querySelectorAll(':scope > a.name[href]')].filter(visible);
          if(names.length!==1) continue;
          const name=names[0];
          const label=clean(name.innerText);
          if(!label||label.length>200||/^(登录|注册|未登录)$/.test(label)) continue;
          let destination;
          try { destination=new URL(name.getAttribute('href'),location.href); }
          catch { continue; }
          if(destination.origin!==location.origin||destination.pathname!=='/personal-center.html'||
              destination.username||destination.password||destination.search||destination.hash) continue;
          const exits=[...dropdown.querySelectorAll(':scope > .dropdown-content.logout > a.dc')];
          if(exits.length!==1||clean(exits[0].textContent)!=='退出'||
              !/^logout\s*\(\s*\)\s*;?$/.test(exits[0].getAttribute('onclick')||'')) continue;
          accounts.push(name);
        }
      }
      return accounts.length===1;
    }"""))
