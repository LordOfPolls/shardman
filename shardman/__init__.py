import asyncio
from datetime import datetime, timedelta, timezone

import ulid
from beanie import init_beanie
from fastapi import Depends, FastAPI, HTTPException, Header
from motor.motor_asyncio import AsyncIOMotorClient

from shardman.config import load_config
from shardman.models import Shard, all_models
from shardman.requests import Heartbeat, SessionID, Register
from shardman.responses import ConnectConfirmed, Status, ShardProjection
from shardman.state import AlertType, StateManager

api = FastAPI(title="Shardman")

config = load_config()

if config.cors_origins:
    from fastapi.middleware.cors import CORSMiddleware

    api.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


state: StateManager = StateManager()

next_halt_time: datetime = None
left_before_halt = 5


@api.on_event("startup")
async def startup():
    global state
    config = load_config()
    client = AsyncIOMotorClient(config.mongodb, tz_aware=True, tzinfo=timezone.utc)
    await init_beanie(database=client[config.database], document_models=all_models)

    await state.get_bot_info()

    loop = asyncio.get_event_loop()
    loop.create_task(state.check_sessions())


async def requires_authorization(
    authorization: str = Header(description="Authorization Token"),  # noqa: B008
):
    config = load_config()
    if authorization != config.secret:
        raise HTTPException(status_code=403, detail="Invalid Token")


@api.get(
    "/connect",
    responses={
        200: {"model": ConnectConfirmed},
        401: {"description": "No Shards Available"},
        403: {"description": "Invalid Token"},
    },
    dependencies=[Depends(requires_authorization)],
)
async def connect() -> ConnectConfirmed:
    global left_before_halt, next_halt_time

    if await Shard.count() >= state.total_shards:
        raise HTTPException(status_code=401, detail="No Shards Available")

    async with state.lock:
        shard_id = await state.get_shard_id()

        session_id = ulid.new().str
        last_beat = datetime.now(tz=timezone.utc)

        sleep_duration = 0.0

        if not next_halt_time or next_halt_time < last_beat:
            next_halt_time = last_beat + timedelta(seconds=5)
            left_before_halt = 5
        if left_before_halt <= 0:
            left_before_halt = 5
            sleep_duration = 5.0
            last_beat = last_beat + timedelta(seconds=5)

        left_before_halt -= 1

        shard = Shard(shard_id=shard_id, session_id=session_id, last_beat=last_beat)
        await shard.insert()

        await state.send_alert(shard=shard, alert_type=AlertType.Connect)

        return ConnectConfirmed(
            shard_id=shard_id,
            max_shards=state.total_shards,
            session_id=session_id,
            sleep_duration=sleep_duration,
        )


@api.post(
    "/beat",
    status_code=204,
    responses={
        401: {"description": "No Shards Available"},
        403: {"description": "Invalid Token"},
        404: {"description": "Session Not Found"},
    },
    dependencies=[Depends(requires_authorization)],
)
async def beat(heartbeat: Heartbeat) -> None:
    shard = await Shard.find_one(Shard.session_id == heartbeat.session_id)
    if not shard:
        raise HTTPException(status_code=404, detail="Session Not Found")
    if shard.shard_id >= state.total_shards:
        raise HTTPException(status_code=401, detail="No Shards Available")

    shard.last_beat = datetime.now(tz=timezone.utc)
    shard.guild_count = heartbeat.guild_count
    shard.latency = heartbeat.latency
    shard.extra = heartbeat.extra
    await shard.save()


@api.get(
    "/disconnect",
    status_code=204,
    responses={
        403: {"description": "Invalid Token"},
        404: {"description": "Session Not Found"},
    },
    dependencies=[Depends(requires_authorization)],
)
async def disconnect(resp: SessionID) -> None:
    shard = await Shard.find_one(Shard.session_id == resp.session_id)
    if not shard:
        raise HTTPException(status_code=404, detail="Session Not Found")

    await shard.delete()


@api.get(
    "/status",
    status_code=200,
)
async def status(extra: bool = False) -> Status:
    shards = await Shard.find_all().project(ShardProjection).to_list()

    return Status(total_shards=state.total_shards, shards=shards)


@api.get("/total_guilds", status_code=200, dependencies=[Depends(requires_authorization)])
async def total_guilds() -> int:
    shards = await Shard.find_all().project(ShardProjection).to_list()
    return sum(shard.guild_count for shard in shards)


@api.get("/gateway_info", status_code=200, dependencies=[Depends(requires_authorization)])
async def gateway_info() -> dict:
    return await state.get_bot_info()

@api.get("/re-register", dependencies=[Depends(requires_authorization)])
async def re_register(payload: Register) -> ConnectConfirmed:
    # used by an already running shard to register itself
    # expects a 2xx response if the shard is available, 4xx if not
    # On 4xx, the shard should stop itself
    missing_shards = await state.get_missing_shards()

    if payload.shard_id not in missing_shards:
        raise HTTPException(status_code=409, detail="Shard Not Available")
    if payload.max_shards != state.total_shards:
        raise HTTPException(status_code=412, detail="Max Shards Mismatch")

    session_id = ulid.new().str
    last_beat = datetime.now(tz=timezone.utc)

    shard = Shard(shard_id=payload.shard_id, session_id=session_id, last_beat=last_beat)
    await shard.insert()

    await state.send_alert(shard=shard, alert_type=AlertType.Connect)

    return ConnectConfirmed(
        shard_id=payload.shard_id,
        max_shards=state.total_shards,
        session_id=session_id,
        sleep_duration=0.0,
    )


