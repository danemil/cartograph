function lineTotal(item) {
  return item.price * item.quantity;
}

function cartTotal(items) {
  return items.reduce((sum, item) => sum + lineTotal(item), 0);
}

module.exports = { lineTotal, cartTotal };
