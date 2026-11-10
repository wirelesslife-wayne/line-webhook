class ActionError(Exception):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status
