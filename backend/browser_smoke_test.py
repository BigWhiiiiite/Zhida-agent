"""Headless end-to-end test for semantic discovery, upload, fill and verification."""
from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from playwright.async_api import async_playwright

from app.browser_models import ExecutePlanRequest, FillAction
from app.browser_service import BrowserDemoService


HTML = """
<!doctype html><html><body><form>
  <label>Full name ✱<input name="full_name" required></label>
  <div class="application-question">
    <div class="application-label">Why do you want this role? ✱</div>
    <div class="application-field"><textarea name="cards[test][field0]" required></textarea></div>
  </div>
  <label>Location ✱<select name="location" required>
    <option value="">Select...</option><option value="CN">China</option>
  </select></label>
  <fieldset class="application-question"><legend>是否接受调剂 ✱</legend>
    <label><input type="radio" name="transfer" value="yes" required style="display:none">是</label>
    <label><input type="radio" name="transfer" value="no" required style="display:none">否</label>
  </fieldset>
  <div class="application-question" role="radiogroup" aria-required="true">
    <div class="application-label">性别 ✱</div>
    <button type="button" role="radio" data-value="male" aria-label="男" aria-checked="false"
      onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(x=>x.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true')">男</button>
    <button type="button" role="radio" data-value="female" aria-label="女" aria-checked="false"
      onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(x=>x.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true')">女</button>
  </div>
  <div class="tencent-custom-question">
    <div>除上述选择外，是否还接受其他城市分配 ✱</div>
    <div class="tencent-choice-options" aria-required="true">
      <span tabindex="0" role="radio" data-value="yes" aria-label="是" aria-checked="false"
        onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(x=>x.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true')">是</span>
      <span tabindex="0" role="radio" data-value="no" aria-label="否" aria-checked="false"
        onclick="this.parentElement.querySelectorAll('[role=radio]').forEach(x=>x.setAttribute('aria-checked','false'));this.setAttribute('aria-checked','true')">否</span>
    </div>
  </div>
  <label>国家/地区 ✱<select name="country" required>
    <option value="">请选择</option><option value="CN">中国大陆</option><option value="SG">新加坡</option>
  </select></label>
  <div class="ant-form-item">
    <div class="ant-form-item-label">当前所处地 ✱</div>
    <div class="ant-select-selector" aria-required="true" aria-controls="current-location-options"
      onclick="document.getElementById('current-location-options').hidden=false">
      <input name="current_location" role="combobox" aria-controls="current-location-options" readonly>
      <span class="selected-value"></span><span>请选择</span>
    </div>
    <div id="current-location-options" hidden>
      <div class="ant-select-item-option" onclick="const root=this.closest('.ant-form-item').querySelector('.ant-select-selector');root.setAttribute('aria-valuetext','北京市');root.querySelector('.selected-value').textContent='北京市';this.parentElement.hidden=true">北京市</div>
      <div class="ant-select-item-option">上海市</div>
    </div>
  </div>
  <div class="moka-form-item">
    <div class="question-title">期望工作城市 ✱</div>
    <small>请从公司开放的真实城市中选择</small>
    <button type="button" role="combobox" aria-label="北京" aria-required="true" aria-controls="preferred-city-options"
      onclick="document.getElementById('preferred-city-options').hidden=false"><input name="preferred_city" readonly></button>
    <div id="preferred-city-options" role="listbox" hidden>
      <button type="button" role="option" onclick="const c=this.closest('.moka-form-item').querySelector('[role=combobox]');c.setAttribute('aria-valuetext','北京市');this.parentElement.hidden=true">北京市</button>
      <button type="button" role="option">上海市</button>
    </div>
  </div>
  <div class="ant-form-item">
    <div class="ant-form-item-label">AI应用技能</div>
    <div class="ant-select-selector multiple" aria-controls="skill-options"
      onclick="document.getElementById('skill-options').hidden=false">
      <input name="ai_skills" role="combobox" aria-controls="skill-options" readonly>
    </div>
    <div id="skill-options" hidden>
      <div class="ant-select-item-option" onclick="const root=this.closest('.ant-form-item').querySelector('.ant-select-selector');if(![...root.querySelectorAll('.selection-item')].some(x=>x.textContent===this.textContent)){const tag=document.createElement('span');tag.className='selection-item';tag.textContent=this.textContent;root.appendChild(tag)}">Python</div>
      <div class="ant-select-item-option" onclick="const root=this.closest('.ant-form-item').querySelector('.ant-select-selector');if(![...root.querySelectorAll('.selection-item')].some(x=>x.textContent===this.textContent)){const tag=document.createElement('span');tag.className='selection-item';tag.textContent=this.textContent;root.appendChild(tag)}">Agent</div>
      <div class="ant-select-item-option">Java</div>
    </div>
  </div>
  <label>语言能力<textarea name="languages"></textarea></label>
  <section class="application-section"><h3>教育经历</h3>
    <div class="education-item"><h4>硕士教育经历</h4>
      <label>院校名称<input name="master_school"></label>
      <label>学院名称<input name="master_college"></label>
      <label>培养方式<input name="master_study_mode"></label>
    </div>
    <div class="education-item"><h4>本科教育经历</h4>
      <label>院校名称<input name="bachelor_school"></label>
      <label>学院名称<input name="bachelor_college"></label>
    </div>
  </section>
  <div class="moka-dynamic-question">
    <div>补充问题：是否接受轮岗</div>
    <div class="select-shell">
      <button id="rotation-control" type="button" role="combobox" aria-controls="rotation-options"
        data-open-count="0"
        onclick="const count=Number(this.dataset.openCount)+1;this.dataset.openCount=String(count);if(count>3)document.getElementById('rotation-options').hidden=false">请选择</button>
    </div>
    <div id="rotation-options" role="listbox" hidden>
      <button type="button" role="option">是</button><button type="button" role="option">否</button>
    </div>
  </div>
  <div><input name="field_7f3a91" placeholder="请输入"></div>
  <section class="application-section">
    <h3>证书</h3>
    <button id="add-certificate" type="button"
      onclick="const label=document.createElement('label');label.textContent='证书名称';const input=document.createElement('input');input.name='certificate_name';label.appendChild(input);this.parentElement.appendChild(label);this.remove()">添加证书</button>
  </section>
  <div class="form-field"><div class="question-label">感兴趣的事业群</div>
    <button id="business-control" type="button" role="combobox" aria-controls="business-options" aria-expanded="false" aria-required="true"
      onclick="document.getElementById('business-options').hidden=false">请选择</button>
    <div id="business-options" role="listbox" hidden>
      <button type="button" role="option" onclick="const c=document.getElementById('business-control');c.textContent='TEG';c.setAttribute('aria-valuetext','TEG');this.parentElement.hidden=true">TEG</button>
      <button type="button" role="option">WXG</button>
    </div>
  </div>
  <label>Emergency contact name<input name="emergency_name"></label>
  <label>Resume/CV ✱<input type="file" name="resume" required style="display:none"></label>
  <button id="parse-resume" type="button" onclick="document.querySelector('[name=full_name]').value='Wrong Candidate';document.querySelector('[name=country]').value='SG';document.querySelector('[name=languages]').value='English'">智能解析</button>
  <button type="button">Submit application</button>
</form></body></html>
"""


async def main() -> None:
    with TemporaryDirectory() as temporary:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(channel="chrome", headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_content(HTML)

            service = BrowserDemoService()
            service.page = page
            service.session_id = "browser-smoke"
            snapshot = await service.snapshot()
            expander = next(field for field in snapshot.fields
                            if field.field_type == "section-button" and "证书" in field.label)
            snapshot = await service.expand_section("browser-smoke", expander.selector)
            assert any(field.name == "certificate_name" for field in snapshot.fields)
            assert not any(field.field_type == "section-button" and "证书" in field.label
                           for field in snapshot.fields)
            by_name = {field.name: field for field in snapshot.fields}
            assert by_name["cards[test][field0]"].label.startswith("Why do you want this role?")
            assert by_name["resume"].field_type == "file"
            radio_fields = [field for field in snapshot.fields if field.name == "transfer"]
            assert {field.option_label for field in radio_fields} == {"是", "否"}
            assert all(field.group_label.startswith("是否接受调剂") for field in radio_fields)
            business = next(field for field in snapshot.fields if "感兴趣的事业群" in field.group_label)
            assert business.options == ["TEG", "WXG"]
            assert business.required
            gender_fields = [field for field in snapshot.fields if field.group_label.startswith("性别")]
            assert len(gender_fields) == 2, [field.model_dump() for field in snapshot.fields]
            assert {field.option_label for field in gender_fields} == {"男", "女"}
            allocation_fields = [field for field in snapshot.fields
                                 if "是否还接受其他城市分配" in field.group_label]
            assert len(allocation_fields) == 2, [field.model_dump() for field in snapshot.fields]
            assert len({field.control_group_key for field in allocation_fields}) == 1
            assert all(field.control_group_key for field in allocation_fields)
            assert {field.option_label for field in allocation_fields} == {"是", "否"}
            assert all(field.options == ["是", "否"] for field in allocation_fields), [
                field.model_dump() for field in allocation_fields
            ]
            country = by_name["country"]
            assert country.options == ["中国大陆", "新加坡"]
            current_location = by_name["current_location"]
            assert current_location.field_type == "combobox"
            assert current_location.options == ["北京市", "上海市"]
            preferred_city = by_name["preferred_city"]
            assert preferred_city.question_text.startswith("期望工作城市"), preferred_city.model_dump()
            assert preferred_city.label_source == "nearby"
            assert preferred_city.recognition_confidence >= .7
            assert "公司开放的真实城市" in preferred_city.help_text
            assert preferred_city.options == ["北京市", "上海市"]
            skills = by_name["ai_skills"]
            assert skills.field_type == "combobox" and skills.multiple
            assert skills.options == ["Python", "Agent", "Java"], skills.model_dump()
            rotation = next(field for field in snapshot.fields if field.name == "rotation-control")
            assert rotation.label.startswith("补充问题：是否接受轮岗"), rotation.model_dump()
            assert rotation.label_source == "nearby"
            opaque = by_name["field_7f3a91"]
            assert opaque.label.startswith("未识别字段 "), opaque.model_dump()
            assert opaque.label_source == "generated"
            master_school = by_name["master_school"]
            master_college = by_name["master_college"]
            master_study_mode = by_name["master_study_mode"]
            bachelor_school = by_name["bachelor_school"]
            assert master_school.semantic_key == "education.school"
            assert master_college.semantic_key == "education.college"
            assert master_study_mode.semantic_key == "education.study_mode"
            assert master_school.entity_scope == "education:master"
            assert master_study_mode.entity_scope == "education:master"
            assert bachelor_school.entity_scope == "education:bachelor"
            assert master_school.container_key == master_college.container_key
            assert master_school.container_key != bachelor_school.container_key
            assert master_school.field_signature and master_school.expected_input
            refreshed = await service.inspect_field("browser-smoke", rotation.selector)
            refreshed_rotation = next(field for field in refreshed.fields if field.name == "rotation-control")
            assert refreshed_rotation.options == ["是", "否"], refreshed_rotation.model_dump()
            assert refreshed_rotation.field_signature == rotation.field_signature
            outline = await page.locator(rotation.selector).evaluate("el => el.style.outline")
            assert "solid" in outline and ("217" in outline or "d99025" in outline), outline

            resume = Path(temporary) / "resume.txt"
            resume.write_text("Test Candidate", encoding="utf-8")
            imported = await service.import_resume_with_site_parser("browser-smoke", resume)
            assert imported.status == "parsed"
            assert imported.trigger_clicked and imported.trigger_label == "智能解析"
            assert imported.changed_fields >= 3
            assert imported.snapshot.fields
            actions = [
                FillAction(selector=by_name["full_name"].selector, label="Full name", action="fill",
                           value="Test Candidate", confidence=1),
                FillAction(selector=by_name["cards[test][field0]"].selector, label="Why this role", action="fill",
                           value="I enjoy building reliable agents.", confidence=1, user_confirmed=True),
                FillAction(selector=by_name["location"].selector, label="Location", action="select",
                           value="China", confidence=1),
                FillAction(selector=next(field for field in radio_fields if field.option_value == "no").selector,
                           label="是否接受调剂", action="check", value=True, confidence=1,
                           user_confirmed=True),
                FillAction(selector=business.selector, label="感兴趣的事业群", action="select",
                           value="TEG", confidence=1, user_confirmed=True),
                FillAction(selector=next(field for field in gender_fields if field.option_label == "男").selector,
                           label="性别", action="check", value=True, confidence=1,
                           sensitive=True, user_confirmed=True),
                FillAction(selector=next(field for field in allocation_fields if field.option_label == "否").selector,
                           label="除上述选择外，是否还接受其他城市分配", action="check", value=True,
                           confidence=1, user_confirmed=True),
                FillAction(selector=country.selector, label="国家/地区", action="select",
                           value="中国", confidence=1),
                FillAction(selector=current_location.selector, label="当前所处地", action="select",
                           value="北京", confidence=1),
                FillAction(selector=preferred_city.selector, label="期望工作城市", action="select",
                           value="北京", confidence=1),
                FillAction(selector=skills.selector, label="AI应用技能", action="select",
                           value="Python, Agent", confidence=1),
                FillAction(selector=by_name["languages"].selector, label="语言能力", action="fill",
                           value="英语", confidence=1),
                FillAction(selector=by_name["emergency_name"].selector, label="Emergency contact name",
                           action="fill", value="Test Candidate", confidence=1),
            ]
            result = await service.execute("browser-smoke", ExecutePlanRequest(actions=actions), resume)
            assert result.failed == 0, [item.model_dump() for item in result.results]
            assert result.verified == 13
            assert result.skipped == 1
            assert await page.locator('[name="emergency_name"]').input_value() == ""
            assert await page.locator(business.selector).get_attribute("aria-valuetext") == "TEG"
            assert await page.locator('[role="radio"][aria-label="男"]').get_attribute("aria-checked") == "true"
            assert await page.locator('[name="country"] option:checked').inner_text() == "中国大陆"
            assert await page.locator('[name="current_location"]').locator("xpath=..").get_attribute("aria-valuetext") == "北京市"
            assert await page.locator('[name="preferred_city"]').locator("xpath=..").get_attribute("aria-valuetext") == "北京市"
            assert await page.locator('.ant-form-item:has-text("AI应用技能") .selection-item').all_text_contents() == ["Python", "Agent"]
            assert result.pre_submit.ready
            assert result.pre_submit.human_challenges == []
            assert result.pre_submit.file_uploads == ["resume.txt"]
            assert result.pre_submit.submit_labels == ["Submit application"]
            await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=\"h-captcha\"></div>')")
            challenge_check = await service.pre_submit_check("browser-smoke")
            assert not challenge_check.ready
            assert challenge_check.human_challenges == ["页面包含需要用户完成的人机验证"]
            await context.close()
            await browser.close()


asyncio.run(main())
print("Zhida browser smoke test passed")
