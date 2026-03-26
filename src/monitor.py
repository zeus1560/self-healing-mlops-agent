import os
import logging


class LogMonitor:
    def __init__(self, log_file_path="data/system_dummy.log"):
        self.log_file_path = log_file_path
        self.last_line_index = 0

        # 💡 윈도우 확장자 에러(.txt)를 막기 위해 파이썬이 직접 진짜 파일을 만듭니다.
        if not os.path.exists(self.log_file_path):
            os.makedirs(os.path.dirname(self.log_file_path), exist_ok=True)
            with open(self.log_file_path, "w", encoding="utf-8") as f:
                f.write("")
            logging.info(
                f"[LogMonitor] 빈 로그 파일이 자동 생성되었습니다: {self.log_file_path}"
            )

        # 파일 줄 수 세기
        with open(self.log_file_path, "r", encoding="utf-8") as f:
            self.last_line_index = len(f.readlines())
        logging.info(
            f"[LogMonitor] 감시 준비. (현재 {self.last_line_index}줄 읽음, 신규 대기 중)"
        )

    def get_recent_errors(self):
        errors = []
        with open(self.log_file_path, "r", encoding="utf-8") as f:
            all_lines = f.readlines()

        # 💡 책갈피 이후에 내용이 추가되었다면
        if len(all_lines) > self.last_line_index:
            new_lines = all_lines[self.last_line_index :]
            self.last_line_index = len(all_lines)  # 책갈피 업데이트

            for line in new_lines:
                if "[ERROR]" in line or "[WARNING]" in line:
                    errors.append(line.strip())

        # 누군가 파일 내용을 싹 지워서 줄어들었다면 리셋
        elif len(all_lines) < self.last_line_index:
            self.last_line_index = len(all_lines)

        return errors
