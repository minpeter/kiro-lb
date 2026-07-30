# -*- coding: utf-8 -*-

"""
Unit-тесты для DebugLogger.
Проверяет логику буферизации и записи debug логов в разных режимах.
"""

import asyncio
import base64
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger as logger_module


class TestDebugLoggerModeOff:
    """Тесты для режима DEBUG_MODE=off."""

    def test_prepare_new_request_does_nothing(self, tmp_path):
        """
        Что он делает: Проверяет, что prepare_new_request ничего не делает в режиме off.
        Цель: Убедиться, что в режиме off директория не создаётся.
        """
        print("Настройка: Режим off...")
        with patch("kiro.debug_logger.DEBUG_MODE", "off"):
            with patch("kiro.debug_logger.DEBUG_DIR", str(tmp_path / "debug_logs")):
                # Пересоздаём экземпляр с новыми настройками
                from kiro.debug_logger import DebugLogger

                logger = DebugLogger.__new__(DebugLogger)
                logger._initialized = False
                logger.__init__()
                logger.debug_dir = tmp_path / "debug_logs"

                print("Действие: Вызов prepare_new_request...")
                logger.prepare_new_request()

                print("Проверяем, что директория не создана...")
                assert not (tmp_path / "debug_logs").exists()

    def test_log_request_body_does_nothing(self, tmp_path):
        """
        Что он делает: Проверяет, что log_request_body ничего не делает в режиме off.
        Цель: Убедиться, что данные не записываются.
        """
        print("Настройка: Режим off...")
        with patch("kiro.debug_logger.DEBUG_MODE", "off"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = tmp_path / "debug_logs"

            print("Действие: Вызов log_request_body...")
            logger.log_request_body(b'{"test": "data"}')

            print("Проверяем, что файл не создан...")
            assert not (tmp_path / "debug_logs" / "request_body.json").exists()


class TestDebugLoggerModeAll:
    """Тесты для режима DEBUG_MODE=all."""

    def test_prepare_new_request_clears_directory(self, tmp_path):
        """
        Что он делает: Проверяет, что prepare_new_request очищает директорию в режиме all.
        Цель: Убедиться, что старые логи удаляются.
        """
        print("Настройка: Режим all, создаём старый файл...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()
        old_file = debug_dir / "old_file.txt"
        old_file.write_text("old content")

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов prepare_new_request...")
            logger.prepare_new_request()

            print("Проверяем, что старый файл удалён...")
            assert not old_file.exists()
            print("Проверяем, что директория существует...")
            assert debug_dir.exists()

    def test_log_request_body_writes_immediately(self, tmp_path):
        """
        Что он делает: Проверяет, что log_request_body пишет сразу в файл в режиме all.
        Цель: Убедиться, что данные записываются немедленно.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_request_body...")
            test_data = b'{"model": "test", "messages": []}'
            logger.log_request_body(test_data)

            print("Проверяем, что файл создан...")
            file_path = debug_dir / "request_body.json"
            assert file_path.exists()

            print("Проверяем содержимое файла...")
            content = json.loads(file_path.read_text())
            assert content["model"] == "test"

    def test_log_kiro_request_body_writes_immediately(self, tmp_path):
        """
        Что он делает: Проверяет, что log_kiro_request_body пишет сразу в файл в режиме all.
        Цель: Убедиться, что Kiro payload записывается немедленно.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_kiro_request_body...")
            test_data = b'{"conversationState": {}}'
            logger.log_kiro_request_body(test_data)

            print("Проверяем, что файл создан...")
            file_path = debug_dir / "kiro_request_body.json"
            assert file_path.exists()

    def test_log_raw_chunk_appends_to_file(self, tmp_path):
        """
        Что он делает: Проверяет, что log_raw_chunk дописывает в файл в режиме all.
        Цель: Убедиться, что чанки накапливаются.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_raw_chunk дважды...")
            logger.log_raw_chunk(b"chunk1")
            logger.log_raw_chunk(b"chunk2")

            print("Проверяем содержимое файла...")
            file_path = debug_dir / "response_stream_raw.txt"
            content = file_path.read_bytes()
            assert content == b"chunk1chunk2"


class TestDebugLoggerModeErrors:
    """Тесты для режима DEBUG_MODE=errors."""

    def test_log_request_body_buffers_data(self, tmp_path):
        """
        Что он делает: Проверяет, что log_request_body буферизует данные в режиме errors.
        Цель: Убедиться, что данные не записываются сразу.
        """
        print("Настройка: Режим errors...")
        debug_dir = tmp_path / "debug_logs"

        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True),
        ):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_request_body...")
            test_data = b'{"test": "buffered"}'
            logger.log_request_body(test_data)

            print("Проверяем, что файл НЕ создан...")
            assert not debug_dir.exists()

            print("Проверяем, что данные в буфере...")
            assert logger._request_body_buffer == test_data

    def test_flush_on_error_writes_buffers(self, tmp_path):
        """
        Что он делает: Проверяет, что flush_on_error записывает буферы в файлы.
        Цель: Убедиться, что при ошибке данные сохраняются.
        """
        print("Настройка: Режим errors, заполняем буферы...")
        debug_dir = tmp_path / "debug_logs"

        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True),
        ):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            # Заполняем буферы
            logger.log_request_body(b'{"request": "body"}')
            logger.log_kiro_request_body(b'{"kiro": "request"}')
            logger.log_raw_chunk(b"raw_chunk")
            logger.log_modified_chunk(b"modified_chunk")

            print("Действие: Вызов flush_on_error...")
            logger.flush_on_error(400, "Bad Request")

            print("Проверяем, что все файлы созданы...")
            assert (debug_dir / "request_body.json").exists()
            assert (debug_dir / "kiro_request_body.json").exists()
            assert (debug_dir / "response_stream_raw.txt").exists()
            assert (debug_dir / "response_stream_modified.txt").exists()
            assert (debug_dir / "error_info.json").exists()

            print("Проверяем error_info.json...")
            error_info = json.loads((debug_dir / "error_info.json").read_text())
            assert error_info["status_code"] == 400
            assert error_info["error_message"] == "Bad Request"

    def test_flush_on_error_clears_buffers(self, tmp_path):
        """
        Что он делает: Проверяет, что flush_on_error очищает буферы после записи.
        Цель: Убедиться, что буферы не накапливаются между запросами.
        """
        print("Настройка: Режим errors...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "errors"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            logger.log_request_body(b'{"test": "data"}')

            print("Действие: Вызов flush_on_error...")
            logger.flush_on_error(500, "Error")

            print("Проверяем, что буферы очищены...")
            assert logger._request_body_buffer is None
            assert logger._kiro_request_body_buffer is None
            assert len(logger._raw_chunks_buffer) == 0
            assert len(logger._modified_chunks_buffer) == 0

    def test_discard_buffers_clears_without_writing(self, tmp_path):
        """
        Что он делает: Проверяет, что discard_buffers очищает буферы без записи.
        Цель: Убедиться, что успешные запросы не оставляют логов.
        """
        print("Настройка: Режим errors, заполняем буферы...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "errors"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            logger.log_request_body(b'{"test": "data"}')
            logger.log_raw_chunk(b"chunk")

            print("Действие: Вызов discard_buffers...")
            logger.discard_buffers()

            print("Проверяем, что директория НЕ создана...")
            assert not debug_dir.exists()

            print("Проверяем, что буферы очищены...")
            assert logger._request_body_buffer is None
            assert len(logger._raw_chunks_buffer) == 0

    def test_flush_on_error_writes_error_info_in_mode_all(self, tmp_path):
        """
        Что он делает: Проверяет, что flush_on_error записывает error_info.json в режиме all.
        Цель: Убедиться, что информация об ошибке сохраняется в обоих режимах.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов flush_on_error...")
            logger.flush_on_error(400, "Bad Request")

            print("Проверяем, что error_info.json создан...")
            assert (debug_dir / "error_info.json").exists()

            print("Проверяем содержимое error_info.json...")
            error_info = json.loads((debug_dir / "error_info.json").read_text())
            assert error_info["status_code"] == 400
            assert error_info["error_message"] == "Bad Request"


class TestDebugLoggerLogErrorInfo:
    """Тесты для метода log_error_info()."""

    def test_log_error_info_writes_in_mode_all(self, tmp_path):
        """
        Что он делает: Проверяет, что log_error_info записывает файл в режиме all.
        Цель: Убедиться, что error_info.json создаётся при ошибках.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_error_info...")
            logger.log_error_info(500, "Internal Server Error")

            print("Проверяем, что error_info.json создан...")
            error_file = debug_dir / "error_info.json"
            assert error_file.exists()

            print("Проверяем содержимое...")
            error_info = json.loads(error_file.read_text())
            assert error_info["status_code"] == 500
            assert error_info["error_message"] == "Internal Server Error"

    def test_log_error_info_writes_in_mode_errors(self, tmp_path):
        """
        Что он делает: Проверяет, что log_error_info записывает файл в режиме errors.
        Цель: Убедиться, что метод работает в обоих режимах.
        """
        print("Настройка: Режим errors...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "errors"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_error_info...")
            logger.log_error_info(404, "Not Found")

            print("Проверяем, что error_info.json создан...")
            error_file = debug_dir / "error_info.json"
            assert error_file.exists()

    def test_log_error_info_does_nothing_in_mode_off(self, tmp_path):
        """
        Что он делает: Проверяет, что log_error_info ничего не делает в режиме off.
        Цель: Убедиться, что в режиме off файлы не создаются.
        """
        print("Настройка: Режим off...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "off"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_error_info...")
            logger.log_error_info(500, "Error")

            print("Проверяем, что директория НЕ создана...")
            assert not debug_dir.exists()


class TestDebugLoggerHelperMethods:
    """Тесты для вспомогательных методов DebugLogger."""

    def test_is_enabled_returns_true_for_errors(self):
        """
        Что он делает: Проверяет _is_enabled() для режима errors.
        Цель: Убедиться, что режим errors считается включённым.
        """
        print("Настройка: Режим errors...")
        with patch("kiro.debug_logger.DEBUG_MODE", "errors"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()

            print("Проверяем _is_enabled()...")
            assert logger._is_enabled() is True

    def test_is_enabled_returns_true_for_all(self):
        """
        Что он делает: Проверяет _is_enabled() для режима all.
        Цель: Убедиться, что режим all считается включённым.
        """
        print("Настройка: Режим all...")
        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()

            print("Проверяем _is_enabled()...")
            assert logger._is_enabled() is True

    def test_is_enabled_returns_false_for_off(self):
        """
        Что он делает: Проверяет _is_enabled() для режима off.
        Цель: Убедиться, что режим off считается выключенным.
        """
        print("Настройка: Режим off...")
        with patch("kiro.debug_logger.DEBUG_MODE", "off"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()

            print("Проверяем _is_enabled()...")
            assert logger._is_enabled() is False

    def test_is_immediate_write_returns_true_for_all(self):
        """
        Что он делает: Проверяет _is_immediate_write() для режима all.
        Цель: Убедиться, что режим all пишет сразу.
        """
        print("Настройка: Режим all...")
        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()

            print("Проверяем _is_immediate_write()...")
            assert logger._is_immediate_write() is True

    def test_is_immediate_write_returns_false_for_errors(self):
        """
        Что он делает: Проверяет _is_immediate_write() для режима errors.
        Цель: Убедиться, что режим errors буферизует.
        """
        print("Настройка: Режим errors...")
        with patch("kiro.debug_logger.DEBUG_MODE", "errors"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()

            print("Проверяем _is_immediate_write()...")
            assert logger._is_immediate_write() is False


class TestDebugLoggerJsonHandling:
    """Тесты для обработки JSON в DebugLogger."""

    def test_log_request_body_formats_json_pretty(self, tmp_path):
        """
        Что он делает: Проверяет, что JSON форматируется красиво.
        Цель: Убедиться, что JSON читаем в файле.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_request_body с JSON...")
            logger.log_request_body(b'{"key":"value"}')

            print("Проверяем форматирование...")
            content = (debug_dir / "request_body.json").read_text()
            # Должен быть отформатирован с отступами
            assert "  " in content or "\n" in content

    def test_log_request_body_handles_invalid_json(self, tmp_path):
        """
        Что он делает: Проверяет обработку невалидного JSON.
        Цель: Убедиться, что невалидный JSON записывается как есть.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            logger = DebugLogger.__new__(DebugLogger)
            logger._initialized = False
            logger.__init__()
            logger.debug_dir = debug_dir

            print("Действие: Вызов log_request_body с невалидным JSON...")
            invalid_data = b"not a json {{"
            logger.log_request_body(invalid_data)

            print("Проверяем, что данные записаны как есть...")
            content = (debug_dir / "request_body.json").read_bytes()
            assert content == invalid_data


class TestDebugLoggerAppLogsCapture:
    """Тесты для захвата логов приложения (app_logs.txt)."""

    def test_prepare_new_request_sets_up_log_capture(self, tmp_path):
        """
        Что он делает: Проверяет, что prepare_new_request настраивает захват логов.
        Цель: Убедиться, что sink для логов создаётся.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            dbg_logger = DebugLogger.__new__(DebugLogger)
            dbg_logger._initialized = False
            dbg_logger.__init__()
            dbg_logger.debug_dir = debug_dir

            print("Действие: Вызов prepare_new_request...")
            dbg_logger.prepare_new_request()

            print("Проверяем, что sink создан...")
            assert dbg_logger._loguru_sink_id is not None

            # Очистка
            dbg_logger._clear_app_logs_buffer()

    def test_flush_on_error_writes_app_logs_in_mode_errors(self, tmp_path):
        """
        Что он делает: Проверяет, что flush_on_error записывает app_logs.txt в режиме errors.
        Цель: Убедиться, что логи приложения сохраняются при ошибках.
        """
        print("Настройка: Режим errors...")
        debug_dir = tmp_path / "debug_logs"

        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True),
        ):
            from kiro.debug_logger import DebugLogger

            dbg_logger = DebugLogger.__new__(DebugLogger)
            dbg_logger._initialized = False
            dbg_logger.__init__()
            dbg_logger.debug_dir = debug_dir

            # Настраиваем захват логов
            dbg_logger.prepare_new_request()

            # Добавляем данные в буфер чтобы flush сработал
            dbg_logger.log_request_body(b'{"test": "data"}')

            # Пишем тестовый лог напрямую в буфер (имитация)
            dbg_logger._app_logs_buffer.write("Test log message\n")

            print("Действие: Вызов flush_on_error...")
            dbg_logger.flush_on_error(500, "Test Error")

            print("Проверяем, что app_logs.txt создан в bundle...")
            captures = [
                path
                for path in (debug_dir / "failures").iterdir()
                if path.is_dir() and not path.name.startswith(".tmp-")
            ]
            assert len(captures) == 1
            app_logs_file = captures[0] / "app_logs.txt"
            assert app_logs_file.exists()

            print("Проверяем содержимое...")
            content = app_logs_file.read_text()
            assert "Test log message" in content

    def test_discard_buffers_saves_logs_in_mode_all(self, tmp_path):
        """
        Что он делает: Проверяет, что discard_buffers сохраняет логи в режиме all.
        Цель: Убедиться, что даже успешные запросы сохраняют логи в режиме all.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            dbg_logger = DebugLogger.__new__(DebugLogger)
            dbg_logger._initialized = False
            dbg_logger.__init__()
            dbg_logger.debug_dir = debug_dir

            # Настраиваем захват логов
            dbg_logger.prepare_new_request()

            # Пишем тестовый лог напрямую в буфер
            dbg_logger._app_logs_buffer.write("Success log message\n")

            print("Действие: Вызов discard_buffers...")
            dbg_logger.discard_buffers()

            print("Проверяем, что app_logs.txt создан...")
            app_logs_file = debug_dir / "app_logs.txt"
            assert app_logs_file.exists()

            print("Проверяем содержимое...")
            content = app_logs_file.read_text()
            assert "Success log message" in content

    def test_discard_buffers_does_not_save_logs_in_mode_errors(self, tmp_path):
        """
        Что он делает: Проверяет, что discard_buffers НЕ сохраняет логи в режиме errors.
        Цель: Убедиться, что успешные запросы не оставляют логов в режиме errors.
        """
        print("Настройка: Режим errors...")
        debug_dir = tmp_path / "debug_logs"

        with patch("kiro.debug_logger.DEBUG_MODE", "errors"):
            from kiro.debug_logger import DebugLogger

            dbg_logger = DebugLogger.__new__(DebugLogger)
            dbg_logger._initialized = False
            dbg_logger.__init__()
            dbg_logger.debug_dir = debug_dir

            # Настраиваем захват логов
            dbg_logger.prepare_new_request()

            # Пишем тестовый лог напрямую в буфер
            dbg_logger._app_logs_buffer.write("Should not be saved\n")

            print("Действие: Вызов discard_buffers...")
            dbg_logger.discard_buffers()

            print("Проверяем, что директория НЕ создана...")
            assert not debug_dir.exists()

    def test_clear_app_logs_buffer_removes_sink(self, tmp_path):
        """
        Что он делает: Проверяет, что _clear_app_logs_buffer удаляет sink.
        Цель: Убедиться, что sink корректно удаляется.
        """
        print("Настройка: Режим all...")
        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            dbg_logger = DebugLogger.__new__(DebugLogger)
            dbg_logger._initialized = False
            dbg_logger.__init__()
            dbg_logger.debug_dir = tmp_path / "debug_logs"

            # Настраиваем захват логов
            dbg_logger.prepare_new_request()
            sink_id = dbg_logger._loguru_sink_id
            assert sink_id is not None

            print("Действие: Вызов _clear_app_logs_buffer...")
            dbg_logger._clear_app_logs_buffer()

            print("Проверяем, что sink_id сброшен...")
            assert dbg_logger._loguru_sink_id is None

    def test_app_logs_not_saved_when_empty(self, tmp_path):
        """
        Что он делает: Проверяет, что пустые логи не создают файл.
        Цель: Убедиться, что app_logs.txt не создаётся если логов нет.
        """
        print("Настройка: Режим all...")
        debug_dir = tmp_path / "debug_logs"
        debug_dir.mkdir()

        with patch("kiro.debug_logger.DEBUG_MODE", "all"):
            from kiro.debug_logger import DebugLogger

            dbg_logger = DebugLogger.__new__(DebugLogger)
            dbg_logger._initialized = False
            dbg_logger.__init__()
            dbg_logger.debug_dir = debug_dir

            # НЕ пишем ничего в буфер

            print("Действие: Вызов _write_app_logs_to_file...")
            dbg_logger._write_app_logs_to_file()

            print("Проверяем, что app_logs.txt НЕ создан...")
            app_logs_file = debug_dir / "app_logs.txt"
            assert not app_logs_file.exists()


def _capture_logger(debug_dir: Path):
    from kiro.debug_logger import DebugLogger

    logger = DebugLogger.__new__(DebugLogger)
    logger._initialized = False
    logger.__init__()
    logger.debug_dir = debug_dir
    return logger


class TestDebugLoggerRequestIsolation:
    @pytest.mark.asyncio
    async def test_concurrent_requests_do_not_mix_buffers(self, tmp_path):
        debug_dir = tmp_path / "debug"
        entered = 0
        entered_lock = asyncio.Lock()
        both_entered = asyncio.Event()

        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)

            async def capture(marker: str):
                nonlocal entered
                logger.prepare_new_request()
                logger.log_request_body(json.dumps({"marker": marker}).encode())
                async with entered_lock:
                    entered += 1
                    if entered == 2:
                        both_entered.set()
                await asyncio.wait_for(both_entered.wait(), timeout=1)
                return logger.flush_on_error(
                    500,
                    "Invalid assistant content event order",
                )

            first, second = await asyncio.gather(
                capture("request-alpha"),
                capture("request-beta"),
            )

        assert first is not None
        assert second is not None
        assert first != second
        first_data = (first / "client_request.json").read_text()
        second_data = (second / "client_request.json").read_text()
        assert ("request-alpha" in first_data) != ("request-alpha" in second_data)
        assert ("request-beta" in first_data) != ("request-beta" in second_data)


class TestDebugLoggerRedaction:
    def test_recursively_redacts_credentials_and_signatures(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_request_body(
                json.dumps(
                    {
                        "content": "preserved prompt",
                        "authorization": "Bearer secret-token",
                        "nested": {
                            "refresh_token": "refresh-secret",
                            "signature": "thinking-signature",
                        },
                    }
                ).encode()
            )
            capture = logger.flush_on_error(500, "stream failure")

        assert capture is not None
        stored = b"\n".join(path.read_bytes() for path in capture.iterdir() if path.is_file())
        assert b"preserved prompt" in stored
        assert b"secret-token" not in stored
        assert b"refresh-secret" not in stored
        assert b"thinking-signature" not in stored

    def test_redacts_camelcase_secrets_and_prefixed_json(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_request_body(
                json.dumps(
                    {
                        "accessToken": "camel-access-secret",
                        "refreshToken": "camel-refresh-secret",
                        "clientSecret": "camel-client-secret",
                        "password": "password-secret",
                    }
                ).encode()
            )
            logger.log_raw_chunk(b'[MCP REQUEST] {"accessToken":"mcp-access-secret"}')
            logger.log_parsed_event({"thinking_signature": "opaque-thinking-signature"})
            capture = logger.flush_on_error(500, "stream failure")

        assert capture is not None
        stored = b"\n".join(path.read_bytes() for path in capture.iterdir() if path.is_file())
        for secret in (
            b"camel-access-secret",
            b"camel-refresh-secret",
            b"camel-client-secret",
            b"password-secret",
            b"mcp-access-secret",
            b"opaque-thinking-signature",
        ):
            assert secret not in stored

    def test_content_disabled_preserves_structure_not_text(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", False, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_request_body(
                json.dumps(
                    {
                        "model": "claude-opus-5",
                        "messages": [
                            {
                                "role": "user",
                                "content": "original private prompt",
                            }
                        ],
                    }
                ).encode()
            )
            capture = logger.flush_on_error(500, "stream failure")

        assert capture is not None
        request_data = json.loads((capture / "client_request.json").read_text())
        assert request_data["model"] == "claude-opus-5"
        assert request_data["messages"][0]["role"] == "user"
        assert request_data["messages"][0]["content"] == {
            "$redacted_text": True,
            "chars": 23,
        }


class TestDebugLoggerBounds:
    def test_total_capture_size_is_bounded(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_raw_chunk(b"prefix-" + (b"x" * 100000) + b"-suffix")
            capture = logger.flush_on_error(500, "stream failure")

        assert capture is not None
        total_size = sum(path.stat().st_size for path in capture.iterdir() if path.is_file())
        assert total_size <= 70000
        manifest = json.loads((capture / "manifest.json").read_text())
        assert manifest["artifacts"]["upstream_chunks.jsonl"]["truncated"] is True

    def test_fragmented_stream_metadata_remains_bounded(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", True, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            for _ in range(10000):
                logger.log_raw_chunk(b"{}")
            capture = logger.flush_on_error(500, "stream failure")

        assert capture is not None
        total_size = sum(path.stat().st_size for path in capture.iterdir() if path.is_file())
        assert total_size <= 70000


class TestDebugLoggerPersistence:
    def test_empty_error_flush_clears_capture_and_removes_sink(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            capture = logger._current_capture()
            sink_id = logger._loguru_sink_id

            assert capture is not None
            assert sink_id is not None
            with patch(
                "kiro.debug_logger.logger.remove",
                wraps=logger_module.remove,
            ) as remove_sink:
                result = logger.flush_on_error(500, "empty failure")

        assert result is None
        assert logger._current_capture() is None
        assert logger._loguru_sink_id is None
        assert capture.loguru_sink_id is None
        remove_sink.assert_called_once_with(sink_id)

    def test_success_capture_failure_does_not_break_request(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_SUCCESS", True, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_request_body(b'{"message":"ok"}')

            with patch(
                "kiro.debug_capture.CaptureState.publish",
                side_effect=OSError("read-only capture directory"),
            ):
                logger.discard_buffers()

        assert logger._current_capture() is None

    def test_startup_removes_only_stale_temporary_directories(self, tmp_path):
        debug_dir = tmp_path / "debug"
        failures = debug_dir / "failures"
        stale = failures / ".tmp-stale"
        fresh = failures / ".tmp-fresh"
        stale.mkdir(parents=True)
        fresh.mkdir()
        old = time.time() - 25 * 60 * 60
        os.utime(stale, (old, old))

        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
        ):
            logger = _capture_logger(debug_dir)

        assert logger.debug_dir == debug_dir
        assert not stale.exists()
        assert fresh.exists()

    def test_publication_failure_removes_temp_dirs_and_prunes_stale(
        self,
        tmp_path,
    ):
        debug_dir = tmp_path / "debug"
        failures = debug_dir / "failures"
        stale = failures / ".tmp-stale"

        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch(
                "kiro.debug_logger.DEBUG_CAPTURE_CONTENT",
                True,
                create=True,
            ),
        ):
            logger = _capture_logger(debug_dir)
            stale.mkdir(parents=True)
            old = time.time() - 25 * 60 * 60
            os.utime(stale, (old, old))
            request_id = logger.prepare_new_request()
            logger.log_request_body(b'{"message":"failure"}')

            with patch(
                "kiro.debug_capture._write_private_file",
                side_effect=OSError("capture write failed"),
            ):
                result = logger.flush_on_error(500, "write failure")

        assert result is None
        assert not stale.exists()
        assert not (failures / f".tmp-{request_id}").exists()
        assert not list(failures.glob(".tmp-*"))

    def test_failure_bundle_is_atomic_private_and_retained(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", False, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 2, create=True),
        ):
            logger = _capture_logger(debug_dir)
            captures = []
            for index in range(3):
                logger.prepare_new_request()
                logger.log_request_body(json.dumps({"index": index}).encode())
                captures.append(logger.flush_on_error(500, f"failure-{index}"))

        completed = [
            path for path in (debug_dir / "failures").iterdir() if path.is_dir() and not path.name.startswith(".tmp-")
        ]
        assert len(completed) == 2
        assert not list((debug_dir / "failures").glob(".tmp-*"))
        for capture in completed:
            assert capture.stat().st_mode & 0o777 == 0o700
            assert all(path.stat().st_mode & 0o777 == 0o600 for path in capture.iterdir() if path.is_file())
        assert captures[-1] in completed


class TestDebugLoggerReplay:
    def test_replay_bundle_preserves_order_and_chunk_boundaries(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", False, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_raw_chunk(b'{"text":"thinking"}')
            logger.log_raw_chunk(b'{"signature":"secret-signature"}')
            logger.log_modified_chunk(b"event: message_start\n")
            logger.log_modified_chunk(b"event: content_block_start\n")
            capture = logger.flush_on_error(
                500,
                "Invalid assistant content event order",
            )

        assert capture is not None
        replay = json.loads((capture / "replay.json").read_text())
        assert [item["seq"] for item in replay["upstream_chunks"]] == [0, 1]
        assert [item["seq"] for item in replay["translated_sse"]] == [2, 3]
        assert "secret-signature" not in json.dumps(replay)

    def test_valid_openai_capture_preserves_terminal_structure(self, tmp_path):
        debug_dir = tmp_path / "debug"
        with (
            patch("kiro.debug_logger.DEBUG_MODE", "errors"),
            patch("kiro.debug_logger.DEBUG_DIR", str(debug_dir)),
            patch("kiro.debug_logger.DEBUG_CAPTURE_CONTENT", False, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_MAX_BYTES", 65536, create=True),
            patch("kiro.debug_logger.DEBUG_CAPTURE_RETENTION", 10, create=True),
        ):
            logger = _capture_logger(debug_dir)
            logger.prepare_new_request()
            logger.log_request_body(b'{"model":"claude-opus-5"}')
            logger.log_modified_chunk(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n')
            logger.log_modified_chunk(b"data: [DONE]\n\n")
            capture = logger.flush_on_error(500, "historical failure")

        assert capture is not None
        replay = json.loads((capture / "replay.json").read_text())
        decoded = b"".join(base64.b64decode(record["payload_base64"]) for record in replay["translated_sse"])
        assert replay["validation"] == {"valid": True, "failure": None}
        assert b'"finish_reason":"stop"' in decoded
        assert b"data: [DONE]" in decoded
