from fastapi import FastAPI, Depends, HTTPException, status, WebSocket, WebSocketDisconnect, Query, Request
from sqlalchemy.orm import Session
from database import SessionLocal, engine
import models, auth, schemas
import json
from sqladmin import Admin, ModelView
from sqlalchemy import func
from datetime import datetime
from fastapi.responses import StreamingResponse
from auth import admin_authentication_backend
import io
import csv

app = FastAPI()

admin = Admin(app, engine, authentication_backend=admin_authentication_backend)

class UserAdmin(ModelView, model=models.User):
    column_list = [models.User.id, models.User.username, models.User.role]

class RoomAdmin(ModelView, model=models.Room):
    column_list = [models.Room.id, models.Room.name, models.Room.description]

class MessageAdmin(ModelView, model=models.Message):
    column_list = [models.Message.id, models.Message.content, models.Message.timestamp]

admin.add_view(UserAdmin)
admin.add_view(RoomAdmin)
admin.add_view(MessageAdmin)

models.Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@app.post("/signup", response_model=schemas.Token)
def signup(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="Username already registered")
    hashed_password = auth.hash_password(user.password)
    new_user = models.User(username=user.username, hashed_password=hashed_password)
    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    access_token = auth.create_access_token(data={"sub": new_user.username, "role": new_user.role})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/login", response_model=schemas.Token)
def login(user: schemas.UserCreate, db: Session = Depends(get_db)):
    db_user = db.query(models.User).filter(models.User.username == user.username).first()
    if not db_user or not auth.verify_password(user.password, db_user.hashed_password):
        raise HTTPException(status_code=401, detail="Incorrect username or password")
    access_token = auth.create_access_token(data={"sub": db_user.username, "role": db_user.role})
    return {"access_token": access_token, "token_type": "bearer"}

@app.get("/analytics/messages_per_room")
def messages_per_room(
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
    _: bool = Depends(admin_authentication_backend), 
):
    query = db.query(
        models.Room.name,
        func.count(models.Message.id).label("message_count")
    ).join(models.Message, models.Room.id == models.Message.room_id)

    if start_date:
        query = query.filter(models.Message.timestamp >= start_date)
    if end_date:
        query = query.filter(models.Message.timestamp <= end_date)

    query = query.group_by(models.Room.name)
    results = query.all()
    return [{"room": name, "message_count": count} for name, count in results]

@app.get("/analytics/user_activity")
def user_activity(
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
    _: bool = Depends(admin_authentication_backend),  
):
    query = db.query(
        models.User.username,
        func.count(models.Message.id).label("message_count")
    ).join(models.Message, models.User.id == models.Message.user_id)

    if start_date:
        query = query.filter(models.Message.timestamp >= start_date)
    if end_date:
        query = query.filter(models.Message.timestamp <= end_date)

    query = query.group_by(models.User.username)
    results = query.all()
    return [{"user": username, "message_count": count} for username, count in results]

class ConnectionManager:
    def __init__(self):
        self.active_connections = {}

    async def connect(self, room_id: str, websocket: WebSocket):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = []
        self.active_connections[room_id].append(websocket)

    def disconnect(self, room_id: str, websocket: WebSocket):
        self.active_connections[room_id].remove(websocket)
        if not self.active_connections[room_id]:
            del self.active_connections[room_id]

    async def broadcast(self, room_id: str, message: str):
        for connection in self.active_connections.get(room_id, []):
            await connection.send_text(message)

manager = ConnectionManager()

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str, token: str = Query(...)):
    payload = auth.decode_access_token(token)
    if payload is None:
        await websocket.close(code=1008)  
        return
    username = payload.get("sub")
    if username is None:
        await websocket.close(code=1008)
        return

    await manager.connect(room_id, websocket)
    db = SessionLocal()

    messages = (
        db.query(models.Message)
        .filter(models.Message.room_id == room_id)
        .order_by(models.Message.timestamp.desc())
        .limit(20)
        .all()
    )
    history = [
        {"user": msg.user.username, "content": msg.content, "timestamp": msg.timestamp.isoformat()}
        for msg in reversed(messages)
    ]
    await websocket.send_text(json.dumps({"type": "history", "messages": history}))

    try:
        while True:
            data = await websocket.receive_text()
            user = db.query(models.User).filter(models.User.username == username).first()

            new_msg = models.Message(room_id=room_id, user_id=user.id, content=data)
            db.add(new_msg)
            db.commit()
            db.refresh(new_msg)

            broadcast_data = json.dumps({
                "type": "message",
                "user": user.username,
                "content": data,
                "timestamp": new_msg.timestamp.isoformat()
            })
            await manager.broadcast(room_id, broadcast_data)

    except WebSocketDisconnect:
        manager.disconnect(room_id, websocket)
        db.close()

@app.get("/analytics/messages_per_room/export")
def export_messages_per_room_csv(
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
    _: bool = Depends(admin_authentication_backend),
):
    query = db.query(
        models.Room.name,
        func.count(models.Message.id).label("message_count")
    ).join(models.Message, models.Room.id == models.Message.room_id)

    if start_date:
        query = query.filter(models.Message.timestamp >= start_date)
    if end_date:
        query = query.filter(models.Message.timestamp <= end_date)

    query = query.group_by(models.Room.name)
    results = query.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["room", "message_count"])
    for name, count in results:
        writer.writerow([name, count])

    output.seek(0)
    return StreamingResponse(output, media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=messages_per_room.csv"
    })

@app.get("/analytics/user_activity/export")
def export_user_activity_csv(
    start_date: datetime | None = Query(None),
    end_date: datetime | None = Query(None),
    db: Session = Depends(get_db),
    _: bool = Depends(admin_authentication_backend),
):
    query = db.query(
        models.User.username,
        func.count(models.Message.id).label("message_count")
    ).join(models.Message, models.User.id == models.Message.user_id)

    if start_date:
        query = query.filter(models.Message.timestamp >= start_date)
    if end_date:
        query = query.filter(models.Message.timestamp <= end_date)

    query = query.group_by(models.User.username)
    results = query.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["user", "message_count"])
    for username, count in results:
        writer.writerow([username, count])

    output.seek(0)
    return StreamingResponse(output, media_type="text/csv", headers={
        "Content-Disposition": "attachment; filename=user_activity.csv"
    })
