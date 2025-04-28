from werkzeug.security import generate_password_hash

password = "12345678"
hashed_password = generate_password_hash(password, method="pbkdf2:sha256")
print(hashed_password)
