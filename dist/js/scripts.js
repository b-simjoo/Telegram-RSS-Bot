function sleep(milliseconds) {
  const date = Date.now();
  let currentDate = null;
  do {
    currentDate = Date.now();
  } while (currentDate - date < milliseconds);
}

function fade(element, callback) {
  var op = 1;  // initial opacity
  var timer = setInterval(function () {
    if (op <= 0) {
      clearInterval(timer);
      callback()
    }
    element.style.opacity = op;
    op -= 0.1;
  }, 50);
}

function unfade(element) {
  var op = 0;  // initial opacity
  element.style.opacity = op;
  element.style.display = 'block';
  var timer = setInterval(function () {
    if (op >= 1) {
      clearInterval(timer);
    }
    element.style.opacity = op;
    op += 0.1;
  }, 50);
}