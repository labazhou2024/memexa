"""
Input Validators - 统一输入验证层

提供所有组件共享的输入验证函数
"""

import re
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple


class ValidationError(ValueError):
    """验证错误异常"""
    pass


# 常用正则表达式
FILE_NAME_PATTERN = re.compile(r'^[a-zA-Z0-9_\-\.]+$')
DATE_PATTERN = re.compile(r'^\d{4}-\d{2}-\d{2}$')
SQL_KEYWORDS = re.compile(
    r'\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|EXECUTE|UNION|'
    r'SCRIPT|JAVASCRIPT|VBSCRIPT|ONERROR|ONLOAD|ONCLICK)\b',
    re.IGNORECASE
)


def validate_task_id(task_id: str) -> str:
    """
    验证任务ID
    
    规则:
    - 长度: 3-64字符
    - 允许: 字母、数字、下划线、连字符
    - 不允许: 特殊字符、SQL关键字
    """
    if not isinstance(task_id, str):
        raise ValidationError(f"task_id must be string, got {type(task_id)}")
    
    if len(task_id) < 3 or len(task_id) > 64:
        raise ValidationError(f"task_id length must be 3-64, got {len(task_id)}")
    
    if not FILE_NAME_PATTERN.match(task_id):
        raise ValidationError(f"task_id contains invalid characters: {task_id}")
    
    return task_id


def validate_agent_name(agent: str) -> str:
    """
    验证Agent名称
    
    规则:
    - 长度: 1-32字符
    - 允许: 字母、数字、下划线
    """
    if not isinstance(agent, str):
        raise ValidationError(f"agent must be string, got {type(agent)}")
    
    if len(agent) < 1 or len(agent) > 32:
        raise ValidationError(f"agent length must be 1-32, got {len(agent)}")
    
    if not re.match(r'^[a-zA-Z0-9_]+$', agent):
        raise ValidationError(f"agent contains invalid characters: {agent}")
    
    return agent


def validate_context_key(key: str) -> str:
    """
    验证上下文键名
    
    规则:
    - 长度: 1-64字符
    - 允许: 字母、数字、下划线、点号
    """
    if not isinstance(key, str):
        raise ValidationError(f"key must be string, got {type(key)}")
    
    if len(key) < 1 or len(key) > 64:
        raise ValidationError(f"key length must be 1-64, got {len(key)}")
    
    if not re.match(r'^[a-zA-Z0-9_\.]+$', key):
        raise ValidationError(f"key contains invalid characters: {key}")
    
    return key


def validate_date_string(date_str: str) -> str:
    """
    验证日期字符串 (YYYY-MM-DD)
    """
    if not isinstance(date_str, str):
        raise ValidationError(f"date must be string, got {type(date_str)}")
    
    if not DATE_PATTERN.match(date_str):
        raise ValidationError(f"date must be YYYY-MM-DD format: {date_str}")
    
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError as e:
        raise ValidationError(f"invalid date: {date_str}, {e}")
    
    return date_str


def validate_days(days: int, min_days: int = 1, max_days: int = 3650) -> int:
    """
    验证天数参数
    
    用于: get_mood_history, cleanup_old_data等
    """
    if not isinstance(days, int):
        raise ValidationError(f"days must be integer, got {type(days)}")
    
    if days < min_days or days > max_days:
        raise ValidationError(
            f"days must be between {min_days}-{max_days}, got {days}"
        )
    
    return days


def validate_file_path(
    path: str,
    must_exist: bool = False,
    base_dir: Optional[Path] = None
) -> Path:
    """
    验证文件路径
    
    规则:
    - 防止路径遍历攻击
    - 可选: 验证文件存在性
    - 可选: 验证在指定基础目录下
    """
    if not isinstance(path, (str, Path)):
        raise ValidationError(f"path must be string or Path, got {type(path)}")
    
    path_obj = Path(path).resolve()
    
    # 路径遍历检查
    if base_dir:
        base = Path(base_dir).resolve()
        try:
            path_obj.relative_to(base)
        except ValueError:
            raise ValidationError(
                f"path must be within {base}, got {path_obj}"
            )
    
    # 存在性检查
    if must_exist and not path_obj.exists():
        raise ValidationError(f"file not found: {path_obj}")
    
    return path_obj


def sanitize_user_input(text: str, max_length: int = 10000) -> str:
    """
    清理用户输入
    
    用于: WebSocket输入、API参数等
    """
    if not isinstance(text, str):
        raise ValidationError(f"input must be string, got {type(text)}")
    
    # 长度限制
    if len(text) > max_length:
        raise ValidationError(
            f"input exceeds max length {max_length}: {len(text)}"
        )
    
    # 移除控制字符（保留换行、制表符）
    sanitized = ''.join(
        char for char in text 
        if char == '\n' or char == '\t' or (ord(char) >= 32 and ord(char) <= 126)
        or ord(char) > 127  # 允许非ASCII字符（如中文）
    )
    
    return sanitized


def validate_websocket_message(data: dict) -> Tuple[str, str]:
    """
    验证WebSocket消息格式
    
    返回: (message_type, content)
    """
    if not isinstance(data, dict):
        raise ValidationError(f"message must be dict, got {type(data)}")
    
    msg_type = data.get('type')
    content = data.get('content')
    
    if not msg_type:
        raise ValidationError("message missing 'type' field")
    
    if not isinstance(msg_type, str):
        raise ValidationError(f"type must be string, got {type(msg_type)}")
    
    if msg_type not in ('chat', 'ping', 'command'):
        raise ValidationError(f"invalid message type: {msg_type}")
    
    if content and not isinstance(content, str):
        raise ValidationError(f"content must be string, got {type(content)}")
    
    return msg_type, content or ""


def check_sql_injection(text: str) -> bool:
    """
    检测潜在的SQL注入攻击
    
    返回: True表示可疑
    """
    if not isinstance(text, str):
        return False
    
    suspicious_patterns = [
        r'--\s*$',  # SQL注释
        r'/\*.*\*/',  # 块注释
        r'\bOR\s+1\s*=\s*1\b',
        r'\bDROP\s+TABLE\b',
        r'\bUNION\s+SELECT\b',
        r'\bEXEC\s*\(',
    ]
    
    for pattern in suspicious_patterns:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    
    return False


def validate_api_key(api_key: str) -> str:
    """
    验证API key格式
    """
    if not isinstance(api_key, str):
        raise ValidationError(f"API key must be string, got {type(api_key)}")
    
    # 基本长度检查
    if len(api_key) < 10:
        raise ValidationError("API key too short (min 10 chars)")
    
    if len(api_key) > 512:
        raise ValidationError("API key too long (max 512 chars)")
    
    return api_key
