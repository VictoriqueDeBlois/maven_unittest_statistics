"""
多进程安全的日志管理工具类

支持特性：
1. 记录日志路径与进程PID的映射关系
2. 多进程安全的日志写入
3. 自动创建和复用日志对象
4. 环境变量控制日志级别和控制台输出
"""

import logging
import os
import threading
from logging.handlers import RotatingFileHandler
from multiprocessing import Manager
from pathlib import Path
from typing import Dict, Optional


class LoggerManager:
    """
    日志管理器 - 单例模式
    
    环境变量配置：
    - LOG_LEVEL: 日志级别 (DEBUG/INFO/WARNING/ERROR/CRITICAL)，默认INFO
    - LOG_CONSOLE: 是否输出到控制台 (true/false/1/0/yes/no)，默认true
    """

    _instance = None
    _instance_lock = threading.Lock()

    # 多进程共享的数据结构
    _manager = None
    _path_to_pids = None  # 日志路径 -> PID集合
    _logger_cache = None  # (日志路径, PID) -> Logger名称
    _mp_lock = None  # 多进程锁

    def __new__(cls):
        """单例模式实现"""
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化日志管理器"""
        # 避免重复初始化
        if hasattr(self, '_initialized'):
            return

        self._initialized = True

        # 初始化多进程管理器
        if LoggerManager._manager is None:
            LoggerManager._manager = Manager()
            LoggerManager._path_to_pids = LoggerManager._manager.dict()
            LoggerManager._logger_cache = LoggerManager._manager.dict()
            LoggerManager._mp_lock = LoggerManager._manager.Lock()

        # 从环境变量读取配置
        self.log_level = self._get_log_level()
        self.console_output = self._get_console_output()

        # 当前进程ID
        self.current_pid = os.getpid()

    def _get_log_level(self) -> int:
        """从环境变量获取日志级别"""
        level_str = os.getenv('LOG_LEVEL', 'INFO').upper()
        level_map = {
            'DEBUG': logging.DEBUG,
            'INFO': logging.INFO,
            'WARNING': logging.WARNING,
            'ERROR': logging.ERROR,
            'CRITICAL': logging.CRITICAL
        }
        return level_map.get(level_str, logging.INFO)

    def _get_console_output(self) -> bool:
        """从环境变量获取是否输出到控制台"""
        console_str = os.getenv('LOG_CONSOLE', 'false').lower()
        return console_str in ('true', '1', 'yes', 'on')

    def _normalize_path(self, log_path: str) -> str:
        """规范化日志路径"""
        return str(Path(log_path).resolve())

    def _create_logger(self, log_path: str, logger_name: str) -> logging.Logger:
        """
        创建新的日志对象
        
        Args:
            log_path: 日志文件路径
            logger_name: 日志器名称
            
        Returns:
            配置好的Logger对象
        """
        # 创建logger
        logger = logging.getLogger(logger_name)
        logger.setLevel(self.log_level)

        # 清除已有的handlers，避免重复
        logger.handlers.clear()

        # 创建日志目录
        log_dir = Path(log_path).parent
        log_dir.mkdir(parents=True, exist_ok=True)

        # 日志格式
        formatter = logging.Formatter(
            fmt='[%(asctime)s] [%(levelname)s] [PID:%(process)d] [%(name)s] - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # 文件处理器 - 使用RotatingFileHandler支持多进程
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=100 * 1024 * 1024,  # 100MB
            backupCount=10,
            encoding='utf-8'
        )
        file_handler.setLevel(self.log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 控制台处理器
        if self.console_output:
            console_handler = logging.StreamHandler()
            console_handler.setLevel(self.log_level)
            console_handler.setFormatter(formatter)
            logger.addHandler(console_handler)

        # 防止日志传播到root logger
        logger.propagate = False

        return logger

    def get_logger(self, log_path: str | Path, name: Optional[str] = None) -> logging.Logger:
        """
        获取或创建日志对象
        
        Args:
            log_path: 日志文件路径
            name: 日志器的自定义名称（可选）
            
        Returns:
            Logger对象
        """
        # 规范化路径
        log_path = self._normalize_path(str(log_path))
        current_pid = self.current_pid

        with LoggerManager._mp_lock:
            # 检查缓存
            cache_key = f"{log_path}:{current_pid}"

            # 情况4: 日志路径存在，且PID也存在 - 返回已有logger
            if cache_key in LoggerManager._logger_cache:
                logger_name = LoggerManager._logger_cache[cache_key]
                logger = logging.getLogger(logger_name)

                # 确保logger有handlers（可能被清除过）
                if not logger.handlers:
                    logger = self._create_logger(log_path, logger_name)

                return logger

            # 生成logger名称
            if name:
                logger_name = f"{name}_pid{current_pid}"
            else:
                # 使用路径和PID生成唯一名称
                path_name = Path(log_path).stem
                logger_name = f"{path_name}_pid{current_pid}"

            # 情况3和5: 日志路径存在但PID不存在 - 多进程场景
            if log_path in LoggerManager._path_to_pids:
                pids = set(LoggerManager._path_to_pids[log_path])
                pids.add(current_pid)
                LoggerManager._path_to_pids[log_path] = list(pids)
            # 情况3: 新的日志路径
            else:
                LoggerManager._path_to_pids[log_path] = [current_pid]

            # 创建新logger
            logger = self._create_logger(log_path, logger_name)

            # 缓存logger信息
            LoggerManager._logger_cache[cache_key] = logger_name

            return logger

    def get_path_info(self, log_path: str) -> Dict:
        """
        获取日志路径的相关信息
        
        Args:
            log_path: 日志文件路径
            
        Returns:
            包含PIDs列表的字典
        """
        log_path = self._normalize_path(log_path)

        with LoggerManager._mp_lock:
            if log_path in LoggerManager._path_to_pids:
                return {
                    'log_path': log_path,
                    'pids': list(LoggerManager._path_to_pids[log_path]),
                    'num_processes': len(LoggerManager._path_to_pids[log_path])
                }
            else:
                return {
                    'log_path': log_path,
                    'pids': [],
                    'num_processes': 0
                }

    def get_all_paths(self) -> Dict[str, list]:
        """
        获取所有日志路径及其关联的PIDs
        
        Returns:
            日志路径到PIDs列表的映射字典
        """
        with LoggerManager._mp_lock:
            return dict(LoggerManager._path_to_pids)

    def cleanup_pid(self, pid: Optional[int] = None):
        """
        清理指定PID的日志缓存（进程结束时调用）
        
        Args:
            pid: 进程ID，默认为当前进程
        """
        if pid is None:
            pid = self.current_pid

        with LoggerManager._mp_lock:
            # 从path_to_pids中移除该PID
            paths_to_remove = []
            for log_path, pids in list(LoggerManager._path_to_pids.items()):
                pids = set(pids)
                if pid in pids:
                    pids.remove(pid)
                    if pids:
                        LoggerManager._path_to_pids[log_path] = list(pids)
                    else:
                        paths_to_remove.append(log_path)

            # 移除没有PID的路径
            for log_path in paths_to_remove:
                del LoggerManager._path_to_pids[log_path]

            # 从logger_cache中移除该PID的条目
            cache_keys_to_remove = [
                key for key in LoggerManager._logger_cache.keys()
                if key.endswith(f":{pid}")
            ]
            for key in cache_keys_to_remove:
                del LoggerManager._logger_cache[key]


# 便捷函数
def get_logger(log_path: str | Path, name: Optional[str] = None) -> logging.Logger:
    """
    便捷函数：获取日志对象
    
    Args:
        log_path: 日志文件路径
        name: 日志器名称（可选）
        
    Returns:
        Logger对象
    """
    manager = LoggerManager()
    return manager.get_logger(log_path, name)


def get_path_info(log_path: str) -> Dict:
    """
    便捷函数：获取日志路径信息
    
    Args:
        log_path: 日志文件路径
        
    Returns:
        包含路径信息的字典
    """
    manager = LoggerManager()
    return manager.get_path_info(log_path)


def cleanup_current_process():
    """便捷函数：清理当前进程的日志缓存"""
    manager = LoggerManager()
    manager.cleanup_pid()
