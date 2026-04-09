import os
import logging
from src.utils.debouncer import LogDebouncer

class LogMonitor:
    def __init__(self, log_file_path="data/system_dummy.log"):
        self.log_file_path = os.path.abspath(log_file_path)
        self.debouncer = LogDebouncer()
        self.last_position = 0
        
        log_dir = os.path.dirname(self.log_file_path)
        if not os.path.exists(log_dir):
            os.makedirs(log_dir)
            
        if not os.path.exists(self.log_file_path):
            with open(self.log_file_path, 'a') as f:
                f.write("")
            
        logging.info(f"📍 [LogMonitor] 감시 시작: {self.log_file_path}")

    def get_recent_errors(self):
        new_errors = []
        try:
            if not os.path.exists(self.log_file_path):
                return []

            with open(self.log_file_path, "r") as f:
                f.seek(0, 2)
                if f.tell() < self.last_position:
                    self.last_position = 0
                
                f.seek(self.last_position)
                for line in f.readlines():
                    line = line.strip()
                    if line and self.debouncer.is_new_error(line):
                        new_errors.append(line)
                
                self.last_position = f.tell()
        except Exception as e:
            logging.error(f"❌ [LogMonitor] 런타임 에러: {e}")
            
        return new_errors
