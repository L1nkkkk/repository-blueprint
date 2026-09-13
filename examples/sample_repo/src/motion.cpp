namespace motion {
struct State { float position; };

float IntegratePosition(float position, float velocity, float dt) {
    return position + velocity * dt;
}

class MovementSystem {
public:
    void Move(float velocity, float dt, State& state) {
        auto next = IntegratePosition(state.position, velocity, dt);
        state.position = next;
    }
};
}
