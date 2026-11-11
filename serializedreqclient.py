class SerializedReqClient:

    def __init__(self, client, lock):
        self.client = client
        self.lock = lock

    def __getattr__(self, method_name):

        # get the requested method from the client
        original_method = getattr(self.client, method_name)

        # if it's not a method: return the attribute instead
        if not callable(original_method):
            return original_method

        # create a replacement method that acquires the lock
        def locked_method(*args, **kwargs):

            with self.lock:
                return original_method(*args, **kwargs)

        return locked_method