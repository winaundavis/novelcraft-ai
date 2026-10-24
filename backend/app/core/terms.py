"""用户协议版本与服务端接受记录口径。

条款正文在 `frontend/src/services/terms.ts`，通过 TermsDialog 展示。
注册和登录均须明确同意；请求须携带实际展示的版本，避免旧页面误记为新版。

改动条款正文时须同步更新前后端版本号。记录仅表示账号最近一次接受的版本
与时间，不证明已经阅读，也不替代其他必要的运营记录。
"""

TERMS_VERSION = "2026-10-18.1"

# 运营方信息（展示在前端条款页；这里保留一份，供服务端日志/审计引用）
TERMS_OPERATOR = "NovelCraft AI 运营团队"
TERMS_CONTACT_EMAIL = "contact@example.com"

# 未勾选时的提示语（前端也有一份同样措辞，保持口径一致）
TERMS_REQUIRED_MESSAGE = "请先阅读并同意《用户协议与免责条款》后再继续。"
TERMS_UPDATED_MESSAGE = "用户协议版本已更新，请刷新页面后重新阅读并同意。"
