class DingTalkCredentialError(Exception):
    """表示一次性钉钉免登凭证无效、过期或已使用，禁止回显原始凭证。"""


class DingTalkUnavailableError(Exception):
    """表示钉钉服务、Redis 协调或应用凭证暂不可用。"""
