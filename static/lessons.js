// Suggested beginner lessons for Tutorial mode, in course order.
// label: what the chip shows · prompt: the lesson topic sent to Claude (kept under 300 characters).
window.LESSON_TOPICS = [
  {
    group: "Start here",
    items: [
      { label: "What is programming?", prompt: "What programming is: a computer follows exact step-by-step instructions. Compare it to a cooking recipe, then show a first one-line Python program print(\"Hello\") and its output. For people who have never coded." },
      { label: "How a computer runs code", prompt: "How a computer runs a program: input, processing (CPU), memory and output, explained with everyday examples, then a tiny Python example that takes a number and prints the double. Total beginners." },
      { label: "What is an algorithm?", prompt: "What an algorithm is: clear steps to solve a problem. Use making tea and finding the biggest number in a list as examples, then show the same idea as short Python code. Total beginners." },
      { label: "Which language to learn first", prompt: "Which programming language to learn first: what Python, JavaScript, HTML/CSS and SQL are each used for, with a one-line example of each, and why Python is a good first choice. Total beginners." },
      { label: "Install Python and VS Code", prompt: "How to set up for coding on a computer: install Python and VS Code, create a file hello.py, write print(\"Hello\"), and run it from the terminal with python hello.py. Show what you see on screen. Total beginners." },
      { label: "How to read error messages", prompt: "How to read a Python error message without fear: the line number, the error type and the message. Show NameError and SyntaxError examples with the broken code, the error output, and the fix. Beginners." },
    ],
  },
  {
    group: "Python basics",
    items: [
      { label: "print() and your first program", prompt: "Python print(): show text and numbers, print several values with commas, and use a new line. Tiny examples with their exact output. Total beginners." },
      { label: "Variables", prompt: "Python variables: what a variable is (a labelled box), how to create one with =, change its value, and print it. Good and bad variable names. Tiny examples with output. Total beginners." },
      { label: "Numbers and math", prompt: "Python numbers and math: + - * /, // and % with simple examples (sharing sweets among friends), integers vs decimals, and the order of operations. Show each result. Total beginners." },
      { label: "Strings (text)", prompt: "Python strings: create text with quotes, join strings with +, len(), upper() and f-strings like f\"Hello {name}\". Tiny examples with exact output. Total beginners." },
      { label: "input(): ask the user", prompt: "Python input(): ask the user for their name and age, print a greeting, and convert text to a number with int(). Show a sample run, including the common mistake of adding a string to a number. Beginners." },
      { label: "if / else decisions", prompt: "Python if, elif and else: make decisions, e.g. check if a number is positive, negative or zero, or if a student passed an exam. Explain indentation. Tiny examples with output. Beginners." },
      { label: "Comparisons and and/or/not", prompt: "Python comparison operators (==, !=, <, >, <=, >=) and logical operators and, or, not, with True/False results and a simple example like checking a valid age range. Beginners." },
      { label: "for loops", prompt: "Python for loops for total beginners: repeat with range(), loop over a list, and use the loop variable. Common mistakes: counting starts at 0, and indentation. Tiny examples with output." },
      { label: "while loops", prompt: "Python while loops: repeat while a condition is true, a countdown example, and how to avoid an infinite loop. Compare with a for loop. Tiny examples with output. Beginners." },
      { label: "Lists", prompt: "Python lists: create a list, get items by index (starting at 0), append, remove, len(), and loop over a list of fruits. Tiny examples with exact output. Beginners." },
      { label: "Dictionaries", prompt: "Python dictionaries: store pairs like a phone book (name to number), read a value by key, add and change items, and loop over keys and values. Tiny examples with output. Beginners." },
      { label: "Functions", prompt: "Python functions: why we use them, define one with def, call it, and pass parameters, e.g. a greet(name) function. Tiny examples with exact output. Beginners." },
      { label: "Return values", prompt: "Python return: the difference between print and return, a function that returns the area of a rectangle, and using the returned value in other code. Tiny examples with output. Beginners." },
      { label: "Common beginner errors", prompt: "The 4 most common Python beginner errors: IndentationError, NameError, TypeError and SyntaxError (a missing colon or quote). For each: broken code, the error message, and the fix. Beginners." },
      { label: "Project: number guessing game", prompt: "Build a small number guessing game in Python step by step: import random, pick a number from 1 to 10, ask the user with input(), say higher or lower with if/else, and repeat with a while loop. Beginners." },
      { label: "Project: simple calculator", prompt: "Build a simple calculator in Python step by step: ask for two numbers and an operator (+ - * /), compute with if/elif, print the result, and handle division by zero. Beginners." },
    ],
  },
  {
    group: "Web basics",
    items: [
      { label: "How websites work", prompt: "How a website works for beginners: browser, URL, server and the HTML/CSS/JavaScript files it sends back. Use ordering food as an everyday comparison and show a tiny HTML file. No prior knowledge." },
      { label: "Your first HTML page", prompt: "Write your first HTML page: the basic structure (html, head, title, body), an h1 heading and a paragraph, save it as index.html and open it in a browser. Show the code and what appears. Beginners." },
      { label: "HTML links and images", prompt: "HTML links and images: the a tag with href, the img tag with src and alt, and a simple list with ul and li. Short code examples and what each shows in the browser. Beginners." },
      { label: "CSS: colors and fonts", prompt: "CSS basics: what CSS does, connect a style to HTML, and change color, background-color, font-size and font-family with selectors like h1 and p. Short examples. Beginners." },
      { label: "CSS box model", prompt: "The CSS box model explained simply: content, padding, border and margin, with a styled card example and how each value changes the size and spacing. Short examples. Beginners." },
      { label: "Flexbox layout", prompt: "CSS Flexbox basics: put boxes in a row with display: flex, then use justify-content, align-items and gap to center and space them. Short code and what it looks like. Beginners." },
      { label: "First JavaScript", prompt: "Your first JavaScript: add a script to a web page, use console.log, and show a message with alert. How to open the browser console to see the output. Short examples. Beginners." },
      { label: "JavaScript variables and functions", prompt: "JavaScript basics: let and const variables, strings and numbers, and a function that adds two numbers. Compare briefly with Python. Short examples with console output. Beginners." },
      { label: "Button click with JavaScript", prompt: "Make a web page button do something with JavaScript: get the button with document.querySelector, add a click event listener, and change a heading's text. Short HTML + JS code. Beginners." },
    ],
  },
  {
    group: "Tools",
    items: [
      { label: "Terminal basics", prompt: "Terminal (command line) basics for beginners on Mac: pwd, ls, cd, mkdir and how to run a Python file. Explain what each command does with its output. Short examples." },
      { label: "What is Git?", prompt: "What Git and version control are for beginners: saving snapshots of your project like save points in a game, and why programmers use it. Show git init and git status with their output." },
      { label: "Git: add, commit, log", prompt: "Basic Git workflow: git add, git commit -m \"message\" and git log, with a tiny project. Explain what each command does and what the output means. Beginners." },
      { label: "Put a project on GitHub", prompt: "Put your first project on GitHub: create a repository, git remote add origin, and git push. Explain what GitHub is and show the commands with their output. Beginners." },
      { label: "VS Code tips", prompt: "VS Code tips for beginners: open a folder, create files, run Python, use the built-in terminal, format code, and find text with Ctrl/Cmd+F. Short and practical." },
    ],
  },
  {
    group: "Concepts",
    items: [
      { label: "Data types", prompt: "Data types in programming for beginners: integers, decimals (float), text (string) and True/False (boolean), with type() in Python and why the type matters. Tiny examples with output." },
      { label: "Binary numbers", prompt: "Binary numbers for beginners: why computers use 0 and 1, count from 0 to 8 in binary, and convert with Python bin() and int('101', 2). Tiny examples with output." },
      { label: "What is an API?", prompt: "What an API is for beginners: like a waiter taking your order to the kitchen. Show a simple example of a request and the JSON response it returns, without needing to install anything." },
      { label: "What is a database?", prompt: "What a database is for beginners: tables with rows and columns like a spreadsheet, and simple SQL: SELECT, WHERE and ORDER BY on a students table. Show each query and its result table." },
      { label: "How the internet sends data", prompt: "How the internet sends data for beginners: IP addresses, packets, DNS turning names into addresses, and what happens when you type a website address. Everyday comparisons, no code needed." },
      { label: "Khmer text in Python", prompt: "Using Khmer text in Python: Khmer strings work with Unicode, print Khmer words, len() of Khmer text, and save Khmer text to a file with encoding=\"utf-8\". Tiny examples with output. Beginners." },
    ],
  },
];
