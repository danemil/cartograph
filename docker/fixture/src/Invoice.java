public class Invoice {
    public int subtotal(int units, int unitPrice) {
        return units * unitPrice;
    }

    public int total(int units, int unitPrice, int tax) {
        return subtotal(units, unitPrice) + tax;
    }
}
