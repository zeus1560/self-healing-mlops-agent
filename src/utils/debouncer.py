class LogDebouncer:
    def __init__(self):
        self.last_error = None

    def is_new_error(self, current_error):
        # 중복된 에러면 False, 새로운 에러면 True 반환
        if current_error == self.last_error:
            return False
        self.last_error = current_error
        return True
