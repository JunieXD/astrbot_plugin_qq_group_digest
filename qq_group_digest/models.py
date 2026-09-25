from dataclasses import asdict, dataclass, field


@dataclass
class Message:
    key: str
    message_id: str
    time: int
    sender: str
    text: str
    seq: int = 0
    forward_ids: list[str] = field(default_factory=list)
    sender_name: str = ""

    def dump(self):
        return asdict(self)


@dataclass(frozen=True)
class Item:
    title: str
    body: str
    sources: tuple[str, ...]

    def dump(self):
        return asdict(self)


@dataclass
class Digest:
    items: list[Item]
    notes: list[str] = field(default_factory=list)

    def dump(self):
        return {"items": [i.dump() for i in self.items], "notes": self.notes}

    @classmethod
    def restore(cls, data):
        return cls(
            [Item(x["title"], x["body"], tuple(x["sources"])) for x in data["items"]], data.get("notes", [])
        )
