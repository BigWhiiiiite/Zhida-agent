"""Shared, scoped option discovery for preview and execution."""
from __future__ import annotations

from uuid import uuid4

from .ats_registry import ATSPolicy


async def visible_popup_ids(page, policy: ATSPolicy) -> list[str]:
    return await page.evaluate("""({selector, prefix}) => {
      return [...document.querySelectorAll(selector)].filter(el => el.getClientRects().length)
        .map((el, i) => {
          if (!el.dataset.zhidaPopup) el.dataset.zhidaPopup = `${prefix}-${i}`;
          return el.dataset.zhidaPopup;
        });
    }""", {"selector": policy.popups, "prefix": uuid4().hex})


async def scoped_option_entries(page, control, policy: ATSPolicy,
                                before_open: list[str] | None = None):
    """No cross-question search. Broken ARIA links fail closed.

    Unlinked portals are allowed only if opening the control created exactly one
    visible popup. Two popups/duplicate captions remain ambiguous.
    """
    items = await control.evaluate(r"""(el, config) => {
      const clean = value => String(value || '').replace(/\s+/g, ' ').trim();
      const visible = node => node.getClientRects().length > 0 && !node.closest('[aria-hidden="true"], [hidden]');
      const enabled = node => !node.matches(':disabled, [aria-disabled="true"], [data-disabled="true"]') &&
        !node.closest('[aria-disabled="true"]') && !/(?:^|[-_\s])disabled(?:$|[-_\s])/.test(String(node.className || ''));
      const optionNodes = root => [...root.querySelectorAll(config.options)]
        .filter(node => visible(node) && enabled(node));
      const carriers = [el, ...el.querySelectorAll('[aria-controls], [aria-owns]')];
      const ids = [...new Set(carriers.flatMap(node =>
        `${node.getAttribute('aria-controls') || ''} ${node.getAttribute('aria-owns') || ''}`.trim().split(/\s+/).filter(Boolean)))];
      let roots = [];
      if (ids.length) {
        roots = ids.map(id => document.getElementById(id)).filter(Boolean);
        if (!roots.length) return [];
      } else {
        const container = el.closest(config.containers || '.form-field, .form-item, .field, fieldset');
        const controls = container ? [...container.querySelectorAll('[role="combobox"], [aria-haspopup="listbox"]')]
          .filter(node => node !== el && !el.contains(node) && !node.contains(el)) : [];
        if (container && !controls.length && optionNodes(container).length) roots = [container];
        else {
          const popups = [...document.querySelectorAll(config.popups)].filter(visible);
          const outer = popups.filter(node => !popups.some(other => other !== node && other.contains(node)));
          roots = outer.filter(node => config.before !== null ?
            !config.before.includes(node.dataset.zhidaPopup) : el.getAttribute('aria-expanded') === 'true');
          if (roots.length !== 1) return [];
        }
      }
      const nodes = [...new Set(roots.flatMap(optionNodes))];
      // Distinct DOM options with the same caption are ambiguous, not duplicates
      // to silently collapse. Containers containing child options are ignored.
      return nodes.filter(node => !nodes.some(other => other !== node && node.contains(other)))
        .map((node, i) => {
          const text = clean(node.innerText || node.textContent || node.value);
          node.dataset.zhidaOption = `${config.prefix}-${i}`;
          return {text, marker: node.dataset.zhidaOption};
        }).filter(item => item.text && item.text.length <= 300 &&
          !/^(请选择|请选择一项|未选择|暂未选择|点击选择|搜索并选择|select|select one|choose|choose one)[.\s…]*$/i.test(item.text));
    }""", {"options": policy.options, "popups": policy.popups,
            "containers": ", ".join((*policy.question_containers, ".form-field", ".form-item", ".field", "fieldset")),
            "before": before_open, "prefix": uuid4().hex})
    return [(item["text"], page.locator(f'[data-zhida-option="{item["marker"]}"]')) for item in items]
