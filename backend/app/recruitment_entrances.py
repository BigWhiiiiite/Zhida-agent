"""Bounded DOM evidence for organization-first recruitment portals.

No company URLs, selectors or destination routes are invented. Organization
choices are shown to the user, never treated as uniquely matched job targets.
"""

ORGANIZATION_ENTRANCES_JS = r'''() => {
  const clean = v => String(v || '').replace(/\s+/g, ' ').trim();
  const visible = el => !!el && !el.closest('[hidden],[aria-hidden="true"]') &&
    el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden' &&
    getComputedStyle(el).display !== 'none';
  const caption = /^(招聘机构|招聘单位|招聘部门|选择招聘机构|选择招聘单位|招聘组织|hiring organizations|recruiting organizations)$/i;
  const organization = /^(?:[\u4e00-\u9fffA-Za-z0-9 ·（）()\-]{0,24})?(?:总行|总部|分行|分公司|子公司|事业部|headquarters|branches|subsidiaries)(?:招聘|职位|岗位)?$/i;
  const forbidden = /提交|投递|申请|协议|同意|登录|注册|隐私|submit|apply|consent|login|sign\s*in/i;
  const textLabel = el => clean(el.innerText || el.getAttribute('aria-label')).replace(/[→➜›»]+$/g, '').trim();
  const interactive = el => el.matches('a[href],button,[role="button"],[onclick]') ||
    (el.hasAttribute('tabindex') && el.tabIndex >= 0) ||
    (getComputedStyle(el).cursor === 'pointer' &&
      (!el.parentElement || getComputedStyle(el.parentElement).cursor !== 'pointer'));
  const entries = new Map();
  const headings = [...document.querySelectorAll('h1,h2,h3,h4,[role="heading"],div,p,span,strong')]
    .filter(el => visible(el) && caption.test(clean(el.innerText)) &&
      !el.closest('nav,header,footer,form,[role="dialog"]') &&
      ![...el.children].some(child => caption.test(clean(child.innerText))));
  for (const heading of headings.slice(0,12)) {
    let region = heading.parentElement;
    for (let depth = 0; region && depth < 4; depth++, region = region.parentElement) {
      if (region.matches('body,html,nav,header,footer,form') || clean(region.innerText).length > 6000) break;
      const labels = [...region.querySelectorAll('a,button,[role="button"],h2,h3,h4,div,p,span,strong')]
        .slice(0,400).filter(el => visible(el) && organization.test(textLabel(el)) &&
          !forbidden.test(textLabel(el)) && !el.closest('form,[role="dialog"]') &&
          ![...el.children].some(child => organization.test(textLabel(child))));
      for (const labelNode of labels) {
        const label = textLabel(labelNode);
        let control = labelNode;
        for (let distance = 0; control && control !== region && distance < 4;
             distance++, control = control.parentElement) {
          if (control.matches('form,nav,header,footer,[role="dialog"]') || !visible(control) ||
              control.disabled || control.form || control.hasAttribute('form') || control.closest('[aria-disabled="true"]')) break;
          const controls = [...control.querySelectorAll('a[href],button,[role="button"],input,select,textarea')];
          // Some portals make only the arrow a real link; its short, owned
          // organization title supplies the label, never a fabricated URL.
          if (!interactive(control) && controls.length !== 1) continue;
          if (controls.length > 1 || controls.some(el => el.matches('input,select,textarea') || forbidden.test(clean(el.innerText)))) break;
          const otherLabels = [...control.querySelectorAll('h2,h3,h4,p,span,strong')]
            .map(textLabel).filter(value => organization.test(value));
          if (new Set(otherLabels).size > 1 || forbidden.test(clean(control.innerText))) break;
          if (control.getAttribute('type') === 'submit' || control.getAttribute('type') === 'reset') break;
          // If the card wraps one real navigation link, click that observed
          // link (e.g. its arrow) rather than an arbitrary parent or centre.
          const target = controls.length === 1 ? controls[0] : control;
          if (visible(target) && !target.disabled && !target.form && !target.hasAttribute('form') &&
              !['submit','reset'].includes(target.getAttribute('type')))
            entries.set(target, {label, section:clean(heading.innerText)});
          break;
        }
      }
      if (entries.size) break;
    }
  }
  return entries;
}'''
