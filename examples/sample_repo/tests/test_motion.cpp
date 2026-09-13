#include "../src/motion.cpp"
#include <cassert>

int main() {
    motion::State state{1.0f};
    motion::MovementSystem movement;
    movement.Move(2.0f, 0.5f, state);
    movement.Move(4.0f, 0.5f, state);
    assert(state.position == 4.0f);
}
