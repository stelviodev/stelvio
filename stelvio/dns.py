class Record:
    def __init__(self, pulumi_resource):
        self._pulumi_resource = pulumi_resource


class Dns:
    def create_record(self, name: str, type: str, value: str) -> Record:
        """
        Create a DNS record with the given name, type, and value.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")

    def create_caa_record(self, name: str, type: str, content: str) -> Record:
        """
        Create a CAA DNS record with the given name, type, and content.
        """
        raise NotImplementedError("This method should be implemented by subclasses.")
