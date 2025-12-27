import asyncio
import httpx
from typing import Optional
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, Depends, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlmodel import SQLModel, Field, select
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from datetime import datetime

engine = create_async_engine(
    'sqlite+aiosqlite:///./tasks.db',
    echo=False
)

DBSession = sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False
)


class TaskModel(SQLModel, table=True):
    __tablename__ = "tasks"
    id: int | None = Field(default=None, primary_key=True)
    title: str
    description: str
    done: bool = False
    created_at: datetime = Field(default_factory=datetime.utcnow)


async def get_db():
    async with DBSession() as session:
        try:
            yield session
        finally:
            await session.close()


app = FastAPI(title='TODO API', version='1.0')


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    async def send_task_update(self, task_data: dict, action: str):
        message = {
            "type": "log",
            "action": action,
            "task": task_data,
        }
        for connection in self.active_connections:
            await connection.send_json(message)

    async def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)


manager = ConnectionManager()


@app.on_event("startup")
async def on_startup():
    async with engine.begin() as conn:
        await conn.run_sync(
            SQLModel.metadata.create_all
        )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"]
)


class TaskCreate(BaseModel):
    title: str
    description: str


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    done: Optional[bool] = None


@app.get("/tasks")
async def get_tasks(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(TaskModel).order_by(TaskModel.created_at.desc()))
    tasks = result.scalars().all()

    task_list = [
        {
            "title": task.title,
            "description": task.description,
            "id": task.id,
            "done": task.done
        }
        for task in tasks
    ]

    await manager.send_task_update(
        task_list,
        "get tasks"
    )

    return tasks


@app.get("/tasks/{task_id}")
async def get_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await db.get(TaskModel, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='Задача не найдена')

    await manager.send_task_update(
        {
            "title": task.title,
            "description": task.description,
            "id": task.id,
            "done": task.done
        },
        "get_task"
    )

    return task


@app.post("/tasks", status_code=201)
async def create_task(
    task: TaskCreate,
    db: AsyncSession = Depends(get_db)
):
    new_task = TaskModel(
        title=task.title,
        description=task.description
    )
    db.add(new_task)
    await db.commit()
    await db.refresh(new_task)

    await manager.send_task_update(
        {
            "title": new_task.title,
            "description": new_task.description,
            "id": new_task.id,
            "done": new_task.done
        },
        "created"
    )

    return new_task


@app.patch("/tasks/{task_id}")
async def update_task(
    task_id: int,
    updated: TaskUpdate,
    db: AsyncSession = Depends(get_db)
):
    task = await db.get(TaskModel, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='Задача не найдена')

    update_data = updated.dict(exclude_unset=True)

    for field, value in update_data.items():
        setattr(task, field, value)

    await db.commit()
    await db.refresh(task)

    await manager.send_task_update(
        {
            "title": task.title,
            "description": task.description,
            "id": task.id,
            "done": task.done
        },
        "updated"
    )

    return task


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: int, db: AsyncSession = Depends(get_db)):
    task = await db.get(TaskModel, task_id)
    if not task:
        raise HTTPException(status_code=404, detail='Задача не найдена')

    task_data = {
        "id": task.id,
        "title": task.title
    }

    await db.delete(task)
    await db.commit()
    await manager.send_task_update(task_data, "deleted")


class JsonPlaceholderParser:
    BASE_URL = "https://jsonplaceholder.typicode.com"

    async def fetch_posts(self, limit) -> list[dict]:
        async with httpx.AsyncClient() as client:
            return (await client.get(f"{self.BASE_URL}/posts?_limit={limit}")).json()


async def add_external_task(title: str, desc: str, db: AsyncSession):
    task = TaskModel(title=title[:100], description=desc[:500])
    db.add(task)
    await db.commit()
    await db.refresh(task)
    await manager.send_task_update(
        { "title": task.title,
          "description": task.description,
          "id": task.id,
          "done": task.done
        }, "created")
    return task


async def periodic_task():
    while True:
        await asyncio.sleep(20)
        posts = await JsonPlaceholderParser().fetch_posts(2)
        async with DBSession() as s:
            [await add_external_task(f"{p['title'][:50]}", p['body'][:150], s) for p in posts]


@app.post("/task-generator/run", status_code=202)
async def run_task_generator():
    asyncio.create_task(periodic_task())
    return {"message": "Фоновая задача запущена"}


@app.on_event("startup")
async def start_background_tasks():
    asyncio.create_task(periodic_task())


@app.websocket("/ws/tasks")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()           
    except WebSocketDisconnect:
        await manager.disconnect(websocket)