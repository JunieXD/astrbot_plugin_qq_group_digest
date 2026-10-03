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
    reply_ids: list[str] = field(default_factory=list)

    def dump(self):
        return asdict(self)


@dataclass(frozen=True)
class Speaker:
    """An explicitly attributed author, independent of their display name."""

    qq: str
    name: str


@dataclass(frozen=True)
class Attribution:
    """Exact display-name span inserted into a resolved item body."""

    start: int
    end: int
    qq: str
    name: str


@dataclass(frozen=True)
class ActivityMember:
    qq: str
    name: str
    count: int


@dataclass(frozen=True)
class Activity:
    total_messages: int
    participants: int
    members: tuple[ActivityMember, ...]
    hourly: tuple[int, ...]

    def dump(self):
        return asdict(self)

    @classmethod
    def restore(cls, data):
        return cls(
            data["total_messages"],
            data["participants"],
            tuple(ActivityMember(**member) for member in data.get("members", [])),
            tuple(data.get("hourly", [0] * 24)),
        )


@dataclass(frozen=True)
class Item:
    title: str
    body: str
    sources: tuple[str, ...]
    speakers: tuple[Speaker, ...] = ()
    body_attributions: tuple[Attribution, ...] = ()

    def dump(self):
        data = asdict(self)
        if not self.speakers:
            data.pop("speakers")
        if not self.body_attributions:
            data.pop("body_attributions")
        return data


@dataclass
class Digest:
    items: list[Item]
    notes: list[str] = field(default_factory=list)
    activity: Activity | None = None

    def dump(self):
        data = {"items": [i.dump() for i in self.items], "notes": self.notes}
        if self.activity is not None:
            data["activity"] = self.activity.dump()
        return data

    @classmethod
    def restore(cls, data):
        return cls(
            [
                Item(
                    x["title"],
                    x["body"],
                    tuple(x.get("sources", [])),
                    tuple(Speaker(**speaker) for speaker in x.get("speakers", [])),
                    tuple(Attribution(**span) for span in x.get("body_attributions", [])),
                )
                for x in data["items"]
            ],
            data.get("notes", []),
            Activity.restore(data["activity"]) if data.get("activity") is not None else None,
        )
