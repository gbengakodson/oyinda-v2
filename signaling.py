from flask_socketio import SocketIO, join_room, leave_room, emit
from flask import request

socketio = SocketIO()

@socketio.on('call-user')
def handle_call(data):
    target_email = data['to']
    # Find target user's socket session (we'll store user_id per session)
    # For simplicity, we'll use the email as room name
    emit('call-made', {
        'from': request.sid,   # caller's session id
        'signal': data['signal']
    }, room=target_email)

@socketio.on('answer-call')
def handle_answer(data):
    emit('call-answered', {
        'signal': data['signal']
    }, room=data['to'])

@socketio.on('ice-candidate')
def handle_ice(data):
    emit('ice-candidate', {
        'candidate': data['candidate']
    }, room=data['to'])