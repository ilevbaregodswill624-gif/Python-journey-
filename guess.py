import random 

secret = random.randint(1, 20)
print("Secret number is", secret)  # This line is for debugging purposes; you can remove it in the final version.
attempts = 0
print("I am thinking of a number between 1 and 20.")

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


guess = int(input("Take a guess: "))

