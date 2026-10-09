import logging

class DuplicateFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.last_messages = {
            logging.CRITICAL: "",
            logging.ERROR: "",
            logging.WARNING: "",
            logging.INFO: "",
            logging.DEBUG: ""
        }

    def filter(self, new_log: logging.LogRecord):
        log_msg = new_log.getMessage()
        last_msg = self.last_messages[new_log.levelno]
        self.last_messages[new_log.levelno] = log_msg

        return not log_msg == last_msg