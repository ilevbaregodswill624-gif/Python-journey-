




























from flask import Flask
app = Flask(__name__)

@app.route("/")
def home():
    return "Hello welcome to my guessing game!"

if __name__=="__main__":
    app.run(debug=True)