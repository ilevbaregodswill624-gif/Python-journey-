import random 
def play_game():
    secret = random.randint(1,20)
    attempts = 0 
    print("Im thinking of a number between 1 and 20.")


    while True:
        guess = int(input("Take a guess: "))
        attempts += 1

        if guess < secret:
            print("Your guess is too low.")
        elif guess > secret:
            print("Your guess is too high.")
        else:
            print("Good job! You guessed my number in", attempts, "attempts!")
            break
        if attempts >= 5:
            print("Sorry, you've used all your attempts. The secret number was", secret)
            break

play_game()

