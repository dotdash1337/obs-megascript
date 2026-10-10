import logging

class DuplicateFilter(logging.Filter):
    def __init__(self):
        super().__init__()

        dupe_msg_data = {
            "msg": "",
            "times_sent": 0
        }

        self.last_messages = {
            logging.CRITICAL: dupe_msg_data.copy(),
            logging.ERROR: dupe_msg_data.copy(),
            logging.WARNING: dupe_msg_data.copy(),
            logging.INFO: dupe_msg_data.copy(),
            logging.DEBUG: dupe_msg_data.copy()
        }

    def filter(self, new_log: logging.LogRecord):
        log_msg = new_log.getMessage()
        last_msg_data = self.last_messages[new_log.levelno]
        last_msg = last_msg_data["msg"]
        # setup last_msg variable and then update the info in the dict
        last_msg_data["msg"] = log_msg
        last_msg_data["times_sent"] = last_msg_data["times_sent"] + 1

        if last_msg == log_msg:
            new_log.msg = new_log.msg + f" (x{last_msg_data["times_sent"]})"
        else:
            last_msg_data["times_sent"] = 1

        return new_log