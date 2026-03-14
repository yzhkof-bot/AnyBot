"""
认证模块 - 简单 Token 认证
防止局域网内未授权访问
"""

import os
import secrets
import hashlib
from pathlib import Path
from typing import Optional

from fastapi import HTTPException, WebSocket, Depends, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from loguru import logger


# Token 配置
_CONFIG_DIR = Path(__file__).parent.parent.parent / ".anybot"
_TOKEN_FILE = _CONFIG_DIR / "token"

# 运行时状态
_token_hash: Optional[str] = None  # 存储 token 的 SHA-256 哈希
_auth_enabled: bool = False

# FastAPI Security
security = HTTPBearer(auto_error=False)


def _hash_token(token: str) -> str:
    """对 token 做 SHA-256 哈希"""
    return hashlib.sha256(token.encode()).hexdigest()


def init_auth(password: Optional[str] = None) -> Optional[str]:
    """初始化认证系统
    
    Args:
        password: 用户指定的密码。如果为 None，则认证关闭。
        
    Returns:
        生效的 token 字符串（仅首次显示），或 None（认证关闭）
    """
    global _token_hash, _auth_enabled

    # 优先使用环境变量
    env_token = os.environ.get("ANYBOT_TOKEN")
    if env_token:
        _token_hash = _hash_token(env_token)
        _auth_enabled = True
        logger.info("🔒 认证已启用 (来自环境变量 ANYBOT_TOKEN)")
        return env_token

    # 使用传入的密码
    if password:
        _token_hash = _hash_token(password)
        _auth_enabled = True
        logger.info("🔒 认证已启用 (来自启动参数)")
        return password

    # 检查已保存的 token 文件
    if _TOKEN_FILE.exists():
        saved_hash = _TOKEN_FILE.read_text().strip()
        if saved_hash:
            _token_hash = saved_hash
            _auth_enabled = True
            logger.info("🔒 认证已启用 (来自已保存的 token)")
            return None  # 已有 token，不再显示

    # 没有配置密码，认证关闭
    _auth_enabled = False
    logger.info("🔓 认证未启用 (可通过 --password 或 ANYBOT_TOKEN 环境变量开启)")
    return None


def generate_and_save_token() -> str:
    """生成随机 token 并保存哈希值到文件"""
    global _token_hash, _auth_enabled

    token = secrets.token_urlsafe(16)  # 22 字符的安全随机字符串
    _token_hash = _hash_token(token)
    _auth_enabled = True

    # 保存哈希（不保存明文）
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(_token_hash)

    return token


def set_password(password: str) -> str:
    """设置密码并保存"""
    global _token_hash, _auth_enabled

    _token_hash = _hash_token(password)
    _auth_enabled = True

    # 保存哈希
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(_token_hash)
    
    logger.info("🔒 密码已更新并保存")
    return password


def verify_token(token: str) -> bool:
    """校验 token 是否有效"""
    if not _auth_enabled:
        return True
    if not token:
        return False
    return _hash_token(token) == _token_hash


def is_auth_enabled() -> bool:
    """返回认证是否启用"""
    return _auth_enabled


# ===== FastAPI 依赖 =====

async def require_auth(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
):
    """REST API 认证依赖 — 注入到需要认证的路由
    
    使用方式: @router.get("/xxx", dependencies=[Depends(require_auth)])
    """
    if not _auth_enabled:
        return  # 认证未开启，直接放行

    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="需要认证，请提供 Token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not verify_token(credentials.credentials):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token 无效",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def verify_ws_token(websocket: WebSocket) -> bool:
    """WebSocket 认证校验
    
    从 URL 查询参数 ?token=xxx 获取 token
    Returns: True 表示通过，False 表示拒绝
    """
    if not _auth_enabled:
        return True

    token = websocket.query_params.get("token", "")
    if not token or not verify_token(token):
        await websocket.close(code=4001, reason="认证失败")
        return False
    return True
