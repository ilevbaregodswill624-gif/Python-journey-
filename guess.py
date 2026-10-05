import random

def play_game(max_number, max_attempts):
    secret = random.randint(1, max_number)
    attempts = 0
    print(f"I'm thinking of a number between 1 and {max_number}.")

    while attempts < max_attempts:
        guess = int(input("Take a guess: "))
        attempts += 1

        if guess < secret:
            print("Too low.")
        elif guess > secret:
            print("Too high.")
        else:
            print(f"Got it in {attempts} attempts!")
            return

    print(f"Out of attempts. The number was {secret}.")

while True:
    level = input("Easy, medium, or hard? ").lower()
    if level == "easy":
        play_game(10, 7)
    elif level == "hard":
        play_game(50, 6)
    else:
        play_game(20, 5)

    if input("Play again? (y/n) ").lower() != "y":
        break