import os
import sys
from types import SimpleNamespace


def test_launcher_imports_only_missing_user_provider_settings(monkeypatch):
    from app.config import load_user_model_environment
    values={"AGENT_MODEL_PROVIDER":"deepseek","DEEPSEEK_API_KEY":"test-user-key","DEEPSEEK_MODEL":"deepseek-flash","DEEPSEEK_BASE_URL":"https://api.deepseek.com"}
    class Key:
        def __enter__(self):return self
        def __exit__(self,*args):pass
    def query_value(_, name):
        if name not in values:
            raise FileNotFoundError(name)
        return values[name], 1
    monkeypatch.setitem(sys.modules,"winreg",SimpleNamespace(HKEY_CURRENT_USER=1,OpenKey=lambda *_:Key(),QueryValueEx=query_value))
    monkeypatch.setattr(os,"name","nt")
    for name in values:monkeypatch.delenv(name,raising=False)
    monkeypatch.setenv("DEEPSEEK_MODEL","process-choice")
    loaded=load_user_model_environment()
    assert os.environ["DEEPSEEK_MODEL"]=="process-choice"
    assert os.environ["DEEPSEEK_API_KEY"]=="test-user-key"
    assert "test-user-key" not in repr(loaded)
    assert "DEEPSEEK_MODEL" not in loaded


def test_test_process_does_not_implicitly_read_user_key(monkeypatch):
    from app.config import load_model_profile_from_environment
    for name in ("AGENT_MODEL_API_KEY","AGENT_MODEL_BASE_URL","AGENT_MODEL_ID","AGENT_MODEL_PROVIDER","DEEPSEEK_API_KEY","OPENAI_API_KEY","ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name,raising=False)
    assert load_model_profile_from_environment() is None
