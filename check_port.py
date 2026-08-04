import socket

s = socket.socket()
try:
    code = s.connect_ex(("127.0.0.1", 5011))
    print("OPEN" if code == 0 else "CLOSED")
finally:
    s.close()
