"""Source-level form-only UI contract; no browser or private data is used."""
from pathlib import Path

source = Path(__file__).with_name("App.tsx").read_text()
connection = Path(__file__).with_name("ExistingSafariConnection.tsx").read_text()
workspace = Path(__file__).with_name("ApplicationWorkspace.tsx").read_text()
setup = source.split("  const submitSetup=", 1)[1].split("  const beginFill=", 1)[0]
primary = source.split("  const beginFill=", 1)[1].split("  const userStatus=", 1)[0]
operation = source.split("  const operation=", 1)[1].split("  const questions=", 1)[0]
assist = source.split("  const assistApplication=", 1)[1].split("  const pauseAssist=", 1)[0]

assert "打开的 Chrome" not in operation and "当前 Chrome" not in operation
assert "网址必须是信息填写页，登录和选岗已由用户完成。" in workspace
assert "label: '投递操作'" in workspace and "label: '开发检查'" in workspace
assert "连接我已登录的 Safari 窗口" in connection
assert "只读取这一标签页的地址，不扫描其他标签页" in connection
assert "api.previewExistingSafari(destination.url)" in connection
assert "data.url!==destination.url" in connection
assert "api.confirmExistingSafari(preview.url,preview.preview_token)" in connection
assert "确认连接这一页" in connection
assert "不会另开窗口" in connection
assert "api.startBrowser(destination.url,undefined,selectedResume,openedSafari?.url===destination.url?openedSafari.token:'')" in source
assert "请先连接你指定的已登录填写窗口，职达不会另开窗口。" in source
assert "已连接你指定的招聘窗口，不会另开或刷新。" in operation
assert "ExternalWebsiteOpening" not in operation
assert "api.openExternalWebsite" not in setup and "api.startBrowser" not in setup and "assistApplication(" not in setup
assert "setWorkspaceStage('workspace')" in setup, "Confirming resume/URL only switches the local page"
assert "if(snapshot){await assistApplication();return}" in primary
assert "const connection=await start()" in primary
assert "if(connection)await assistApplication(connection)" in primary, "First click passes the freshly connected context explicitly"
assert "connection?.snapshot??observationState.current.snapshot" in assist
assert "formStageReady(connection.workflow,currentSnapshot)" in assist
assert "if(blocker||!currentSnapshot)" in assist, "A connected non-form page still cannot fill"
assert "allowSiteParse&&snapshot.browser_engine!=='safari'" in assist
assert "附件、照片请在官网上传" in operation
assert "未上传的材料显示为已完成" in operation
assert "允许本轮自动填写本人身份证号码" not in source
assert "deferGovernmentId" not in source and "allowGovernmentId" not in source
assert "onKeyDown={e=>e.key==='Enter'&&start()}" not in source
assert "不会点击招聘官网的最终提交，不会替你同意声明。" in operation
assert "navigateCandidate" not in operation and "runJourney" not in operation and "选择招聘机构" not in operation
print("workbench_safari_ui_test: OK (explicit existing-window binding, no duplicate window, local setup, explicit first-fill context, manual attachments)")
